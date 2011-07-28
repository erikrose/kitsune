import sys
from sqlalchemy.connectors import Connector

class ZxJDBCConnector(Connector):
    driver = 'zxjdbc'
    
    supports_sane_rowcount = False
    supports_sane_multi_rowcount = False
    
    supports_unicode_binds = True
    supports_unicode_statements = sys.version > '2.5.0+'
    description_encoding = None
    default_paramstyle = 'qmark'
    
    jdbc_db_name = None
    jdbc_driver_name = None
    
    @classmethod
    def dbapi(cls):
        from com.ziclix.python.sql import zxJDBC
        return zxJDBC

    def _driver_kwargs(self):
        """Return kw arg dict to be sent to connect()."""
        return {}
        
    def _create_jdbc_url(self, url):
        """Create a JDBC url from a :class:`~sqlalchemy.engine.url.URL`"""
        return 'jdbc:%s://%s%s/%s' % (self.jdbc_db_name, url.host,
                                      url.port is not None 
                                        and ':%s' % url.port or '',
                                      url.database)
        
    def create_connect_args(self, url):
        opts = self._driver_kwargs()
        opts.update(url.query)
        return [
                [self._create_jdbc_url(url), 
                url.username, url.password, 
                self.jdbc_driver_name],
                opts]

    def is_disconnect(self, e):
        if not isinstance(e, self.dbapi.ProgrammingError):
            return False
        e = str(e)
        return 'connection is closed' in e or 'cursor is closed' in e

    def _get_server_version_info(self, connection):
        # use connection.connection.dbversion, and parse appropriately
        # to get a tuple
        raise NotImplementedError()
