"""
About our little nonce dance
----------------------------

We don't want anybody to be able to impersonate anybody else in a chat. Thus,
we have to map a socketio SID to a user. To do that, we have to map a socketio
SID to a user. So, when we draw the chat page, we make up a nonce and stick it
in there, while simultaneously mapping it to the user ID in redis for a short
time. The JS yanks the nonce out of the page and sends it back to the server on
join, along with the socketio SID (implicitly), thus completing the SID-to-user
mapping. And, because we avoid sticking the session ID in the page, we avoid
the possibility of session hijacking by any JS that gets injected by an XSS
attack; cookies are protected from evil JS better (through HttpOnly, for
example).


About the protocol
------------------

There are several kinds of message which go flitting between the clients and
server. Each message is a JSON dict having the keys listed below:

    Join. Sent from the client to the server.
        kind: join
        room: ID of the room to join
        user: Username of user joining (sent toward client only)

    Leave. Sent in both directions.
        kind: leave
        room: ID of the room
        user: Username of user leaving (sent toward client only)

    Say. Sent in both directions.
        kind: say
        room: ID of the room
        message: What was said
        user: Username of user speaking (sent toward client only)

    Nonce. Identify the user to the server by sending a nonce value.
        kind: nonce
        nonce: the nonce

There's no facility for private messages yet, but it can be added if needed. At
the moment, the plan is to implement private help sessions by first creating a
public room behind the scenes. That will allow additional moderators to steal
into the room undetected.

"""
import json
from string import ascii_letters
import random

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.http import HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from gevent import Greenlet
import jingo

from chat import log, redis
from chat.models import NamedAnonymousUser


@require_GET
def chat(request):
    """Display the current state of the chat queue."""
    nonce = None
    if request.user.is_authenticated():
        nonce = ''.join(random.choice(ascii_letters) for _ in xrange(10))
        redis().setex(_nonce_key(nonce), request.user.id, 60)
    return jingo.render(request, 'chat/chat.html', {'nonce': nonce})


@never_cache
@require_GET
def queue_status(request):
    """Dump the queue status out of the cache.

    See chat.crons.get_queue_status.

    """
    xml = cache.get(settings.CHAT_CACHE_KEY)
    status = 200
    if not xml:
        xml = ''
        status = 503
    return HttpResponse(xml, mimetype='application/xml', status=status)


def _nonce_key(nonce):
    """Return the redis key for storing the nonce-to-user-ID mapping."""
    return 'chatnonce:{n}'.format(n=nonce)


def _random_nick():
    """Return a fun random name to distinguish anonymous users memorably

    This will go away once we get UI for entering a name.

    """
    syllables = ['nox', 'frot', 'gos', 'ler', 'jam', 'rip', 'kap']
    return ''.join(random.choice(syllables) for _ in xrange(3))


def _messages_while_connected(io):
    """Return an iterator over decoded JSON blobs from a socketio socket.

    Stop when the socket disconnects.

    """
    while io.connected():
        message = io.recv()
        # message is always a list of 0 or 1 strings, I deduce from the source.
        if message:
            json_data = message[0]
            log.debug('Outgoing: %s' % json_data)
            try:
                data = json.loads(json_data)
            except ValueError:
                log.warning('Invalid JSON from chat client: %s' % json_data)
            else:
                yield data


def _subscriber(io, channel):
    """Event loop which listens on a redis channel and sends anything
    received to the client JS."""
    try:
        redis_in = redis()
        redis_in.subscribe(channel)
        redis_in_generator = redis_in.listen()

        # io.connected() never becomes false for some reason.
        while io.connected():
            for from_redis in redis_in_generator:
                log.debug('Incoming: %s' % from_redis)

                # This check for 'message' is for redis's idea of a
                # message, not our chat 'message':
                if from_redis['type'] == 'message':
                    # Incoming data should always be valid, as it is checked
                    # before being put into redis. It also happens to be in the
                    # same format the JS expects. Just add the room to it, and
                    # ship it out:
                    out = json.loads(from_redis['data'])
                    out['room'] = channel
                    io.send(json.dumps(out))
    finally:
        log.debug('EXIT SUBSCRIBER %s' % io.session)


def _tell_room(room, **kwargs):
    """Throw a JSON-coded message (from kwargs) at a room."""
    redis().publish(room, json.dumps(kwargs))


def _leave_room(room, username, subscribers):
    """Announce a user is leaving a room, and stop subscribing to it."""
    # Each time I close the 2nd chat window, wait for the old socketio() view
    # to exit, and then reopen the chat page, the number of Incomings increases
    # by one. The subscribers are never exiting. kill() fixes that behavior:
    subscribers[room].kill()
    del subscribers[room]
    _tell_room(room, kind='leave', user=username)


def socketio(io):
    """View that serves the /socket.io madness used by socketio

    Directly handles traffic from the user to the server.

    """
    if io.on_connect():
        log.debug('CONNECT %s' % io.session)
    else:
        # I haven't nailed down when this happens yet. It might happen when
        # websockets are enabled and we hit the server with FF 7.
        log.debug('SOMETHING OTHER THAN CONNECT!')

    # Hanging onto these greenlets might keep them from the GC:
    subscribers = {}  # room name -> subscriber greenlet
    # Start anonymous. JS will immediately authenticate and fix that if the
    # user is logged in.
    user = NamedAnonymousUser(_random_nick())

    # Until the client disconnects, listen for input from the user:
    for from_client in _messages_while_connected(io):
        kind = from_client.get('kind')
        if kind == 'say':
            room = from_client.get('room')
            message = from_client.get('message')
            if room and message:  # else drop it on the floor
                if room in subscribers:
                    _tell_room(room, kind='say',
                                     message=message,
                                     user=user.username)
                else:
                    log.warning("%s said something in a room he wasn't in." %
                                user.username)
        elif kind == 'nonce':
            nonce = from_client.get('nonce')
            if nonce:
                user_id = redis().get(_nonce_key(nonce))
                if user_id:
                    try:
                        user = User.objects.get(id=user_id)
                    except User.DoesNotExist:
                        pass  # Just stay anonymous.
                        # TODO: Think about reloading the page or prompting to.
                    else:
                        log.debug('Mapped nonce to %s.' % user.username)
        elif kind == 'join':
            # Start a new subscriber. At least as a starting point, we start a
            # new subscriber for each room you're in, because otherwise there
            # would be a mess of de-duping as we fight with the race of killing
            # the old subscriber and starting a new one that listens to all
            # joined rooms.
            room = from_client.get('room')
            if room and room not in subscribers:  # No joining a room twice.
                subscribers[room] = Greenlet.spawn(_subscriber, io, room)
                _tell_room(room, kind='join', user=user.username)
        elif kind == 'leave':
            room = from_client.get('room')
            if room and room in subscribers:  # No leaving rooms you're not in.
                _leave_room(r, user.username, subscribers)

    log.debug('EXIT %s' % io.session)

    for r, s in subscribers.items():  # Can't be iteritems; _leave_rooms()
                                      # mutates subscribers.
        _leave_room(r, user.username, subscribers)

    return HttpResponse()
