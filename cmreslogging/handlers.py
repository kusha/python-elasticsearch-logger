""" Elasticsearch logging handler
"""

import logging
import datetime
import socket
import os
import time
from threading import Timer, Lock
from multiprocessing import Process
from enum import Enum
from elasticsearch import helpers as eshelpers
from elasticsearch import Elasticsearch, RequestsHttpConnection

try:
    from requests_kerberos import HTTPKerberosAuth, DISABLED
    CMR_KERBEROS_SUPPORTED = True
except ImportError:
    CMR_KERBEROS_SUPPORTED = False

try:
    from requests_aws4auth import AWS4Auth
    AWS4AUTH_SUPPORTED = True
except ImportError:
    AWS4AUTH_SUPPORTED = False

from cmreslogging.serializers import CMRESSerializer


class CMRESHandler(logging.Handler):
    """ Elasticsearch log handler

    Allows to log to elasticsearch into json format.
    All LogRecord fields are serialised and inserted
    """

    class AuthType(Enum):
        """ Authentication types supported

        The handler supports
         - No authentication
         - Basic authentication
         - Kerberos or SSO authentication (on windows and linux)
         - Authentication using certificate
        """
        NO_AUTH = 0
        BASIC_AUTH = 1
        KERBEROS_AUTH = 2
        AWS_SIGNED_AUTH = 3
        CERT_AUTH = 4

    class IndexNameFrequency(Enum):
        """ Index type supported
        the handler supports
        - Daily indices
        - Weekly indices
        - Monthly indices
        - Year indices
        """
        DAILY = 0
        WEEKLY = 1
        MONTHLY = 2
        YEARLY = 3

    # Defaults for the class
    __DEFAULT_ELASTICSEARCH_HOST = [{'host': 'localhost', 'port': 9200}]
    __DEFAULT_AUTH_USER = ''
    __DEFAULT_AUTH_PASSWD = ''
    __DEFAULT_AWS_ACCESS_KEY = ''
    __DEFAULT_AWS_SECRET_KEY = ''
    __DEFAULT_AWS_REGION = ''
    __DEFAULT_USE_SSL = False
    __DEFAULT_VERIFY_SSL = True
    __DEFAULT_AUTH_TYPE = AuthType.NO_AUTH
    __DEFAULT_INDEX_FREQUENCY = IndexNameFrequency.DAILY
    __DEFAULT_BUFFER_SIZE = 1000
    __DEFAULT_FLUSH_FREQ_INSEC = 1
    __DEFAULT_ADDITIONAL_FIELDS = {}
    __DEFAULT_ES_INDEX_NAME = 'python_logger'
    __DEFAULT_ES_DOC_TYPE = 'python_log'
    __DEFAULT_RAISE_ON_EXCEPTION = False
    __DEFAULT_TIMESTAMP_FIELD_NAME = "timestamp"
    __DEFAULT_ES_RETRY_ON_TIMEOUT = False
    __DEFAULT_ES_TIMEOUT = 10

    __LOGGING_FILTER_FIELDS = ['msecs',
                               'relativeCreated',
                               'levelno',
                               'created']

    @staticmethod
    def _get_daily_index_name(es_index_name):
        """ Returns elasticearch index name
        :param: index_name the prefix to be used in the index
        :return: A srting containing the elasticsearch indexname used which should include the date.
        """
        return "{0!s}-{1!s}".format(es_index_name, datetime.datetime.now().strftime('%Y.%m.%d'))

    @staticmethod
    def _get_weekly_index_name(es_index_name):
        """ Return elasticsearch index name
        :param: index_name the prefix to be used in the index
        :return: A srting containing the elasticsearch indexname used which should include the date and specific week
        """
        current_date = datetime.datetime.now()
        start_of_the_week = current_date - datetime.timedelta(days=current_date.weekday())
        return "{0!s}-{1!s}".format(es_index_name, start_of_the_week.strftime('%Y.%m.%d'))

    @staticmethod
    def _get_monthly_index_name(es_index_name):
        """ Return elasticsearch index name
        :param: index_name the prefix to be used in the index
        :return: A srting containing the elasticsearch indexname used which should include the date and specific moth
        """
        return "{0!s}-{1!s}".format(es_index_name, datetime.datetime.now().strftime('%Y.%m'))

    @staticmethod
    def _get_yearly_index_name(es_index_name):
        """ Return elasticsearch index name
        :param: index_name the prefix to be used in the index
        :return: A srting containing the elasticsearch indexname used which should include the date and specific year
        """
        return "{0!s}-{1!s}".format(es_index_name, datetime.datetime.now().strftime('%Y'))

    _INDEX_FREQUENCY_FUNCION_DICT = {
        IndexNameFrequency.DAILY: _get_daily_index_name,
        IndexNameFrequency.WEEKLY: _get_weekly_index_name,
        IndexNameFrequency.MONTHLY: _get_monthly_index_name,
        IndexNameFrequency.YEARLY: _get_yearly_index_name
    }

    def __init__(self,
                 hosts=__DEFAULT_ELASTICSEARCH_HOST,
                 auth_details=(__DEFAULT_AUTH_USER, __DEFAULT_AUTH_PASSWD),
                 aws_access_key=__DEFAULT_AWS_ACCESS_KEY,
                 aws_secret_key=__DEFAULT_AWS_SECRET_KEY,
                 aws_region=__DEFAULT_AWS_REGION,
                 auth_type=__DEFAULT_AUTH_TYPE,
                 use_ssl=__DEFAULT_USE_SSL,
                 verify_ssl=__DEFAULT_VERIFY_SSL,
                 buffer_size=__DEFAULT_BUFFER_SIZE,
                 flush_frequency_in_sec=__DEFAULT_FLUSH_FREQ_INSEC,
                 es_index_name=__DEFAULT_ES_INDEX_NAME,
                 index_name_frequency=__DEFAULT_INDEX_FREQUENCY,
                 es_doc_type=__DEFAULT_ES_DOC_TYPE,
                 es_additional_fields=__DEFAULT_ADDITIONAL_FIELDS,
                 raise_on_indexing_exceptions=__DEFAULT_RAISE_ON_EXCEPTION,
                 default_timestamp_field_name=__DEFAULT_TIMESTAMP_FIELD_NAME,
                 es_retry_on_timeout=__DEFAULT_ES_RETRY_ON_TIMEOUT,
                 es_timeout=__DEFAULT_ES_TIMEOUT,
                 client_cert=None,
                 ca_certs=None):
        """ Handler constructor

        :param hosts: The list of hosts that elasticsearch clients will connect. The list can be provided
                    in the format ```[{'host':'host1','port':9200}, {'host':'host2','port':9200}]``` to
                    make sure the client supports failover of one of the instertion nodes
        :param auth_details: When ```CMRESHandler.AuthType.BASIC_AUTH``` is used this argument must contain
                    a tuple of string with the user and password that will be used to authenticate against
                    the Elasticsearch servers, for example```('User','Password')
        :param aws_access_key: When ```CMRESHandler.AuthType.AWS_SIGNED_AUTH``` is used this argument must contain
                    the AWS key id of the  the AWS IAM user
        :param aws_secret_key: When ```CMRESHandler.AuthType.AWS_SIGNED_AUTH``` is used this argument must contain
                    the AWS secret key of the  the AWS IAM user
        :param aws_region: When ```CMRESHandler.AuthType.AWS_SIGNED_AUTH``` is used this argument must contain
                    the AWS region of the  the AWS Elasticsearch servers, for example```'us-east'
        :param auth_type: The authentication type to be used in the connection ```CMRESHandler.AuthType```
                    Currently, NO_AUTH, BASIC_AUTH, KERBEROS_AUTH are supported
        :param use_ssl: A boolean that defines if the communications should use SSL encrypted communication
        :param verify_ssl: A boolean that defines if the SSL certificates are validated or not
        :param buffer_size: An int, Once this size is reached on the internal buffer results are flushed into ES
        :param flush_frequency_in_sec: A float representing how often and when the buffer will be flushed, even
                    if the buffer_size has not been reached yet
        :param es_index_name: A string with the prefix of the elasticsearch index that will be created. Note a
                    date with YYYY.MM.dd, ```python_logger``` used by default
        :param index_name_frequency: Defines what the date used in the postfix of the name would be. available values
                    are selected from the IndexNameFrequency class (IndexNameFrequency.DAILY,
                    IndexNameFrequency.WEEKLY, IndexNameFrequency.MONTHLY, IndexNameFrequency.YEARLY). By default
                    it uses daily indices.
        :param es_doc_type: A string with the name of the document type that will be used ```python_log``` used
                    by default
        :param es_additional_fields: A dictionary with all the additional fields that you would like to add
                    to the logs, such the application, environment, etc.
        :param raise_on_indexing_exceptions: A boolean, True only for debugging purposes to raise exceptions
                    caused when
        :param es_retry_on_timeout: A bool value, passed to Elasticsearch object. Specifies if bulk_send retry
                    sending logs after timeout.
        :param es_timeout: An integer value, in seconds, passed to Elasticsearch object. Specifies client-side
                    timeout when performing bulk_send.
        :param client_cert: String (path to a cert) or tuple (in the case of cert + key). Passed to requests session.
        :param ca_certs: String, path to CA certificate (or a bundle).
        :return: A ready to be used CMRESHandler.
        """
        logging.Handler.__init__(self)

        self.hosts = hosts
        self.auth_details = auth_details
        self.aws_access_key = aws_access_key
        self.aws_secret_key = aws_secret_key
        self.aws_region = aws_region
        self.auth_type = auth_type
        self.use_ssl = use_ssl
        self.verify_certs = verify_ssl
        self.buffer_size = buffer_size
        self.flush_frequency_in_sec = flush_frequency_in_sec
        self.es_index_name = es_index_name
        self.index_name_frequency = index_name_frequency
        self.es_doc_type = es_doc_type
        self.es_additional_fields = es_additional_fields.copy()
        self.es_additional_fields.update({'host': socket.gethostname(),
                                          'host_ip': socket.gethostbyname(socket.gethostname())})
        self.raise_on_indexing_exceptions = raise_on_indexing_exceptions
        self.default_timestamp_field_name = default_timestamp_field_name
        self.es_retry_on_timeout = es_retry_on_timeout
        self.es_timeout = es_timeout
        self.client_cert = client_cert
        self.ca_certs = ca_certs

        self._client = None
        self._buffer = []
        self._buffer_lock = Lock()
        self._timer = None
        self._index_name_func = CMRESHandler._INDEX_FREQUENCY_FUNCION_DICT[self.index_name_frequency]
        self.serializer = CMRESSerializer()

    def __schedule_flush(self):
        if self._timer is None:
            self._timer = Timer(self.flush_frequency_in_sec, self.flush)
            self._timer.setDaemon(True)
            self._timer.start()

    def __get_es_client(self):
        if self.auth_type in [CMRESHandler.AuthType.NO_AUTH, CMRESHandler.AuthType.CERT_AUTH]:
            if self.auth_type == CMRESHandler.AuthType.CERT_AUTH and self.client_cert is None:
                raise ValueError("client_cert parameter is required for CERT_AUTH")
            if self._client is None:
                self._client = Elasticsearch(hosts=self.hosts,
                                             use_ssl=self.use_ssl,
                                             verify_certs=self.verify_certs,
                                             connection_class=RequestsHttpConnection,
                                             serializer=self.serializer,
                                             retry_on_timeout=self.es_retry_on_timeout,
                                             timeout=self.es_timeout,
                                             client_cert=self.client_cert,
                                             ca_certs=self.ca_certs)
            return self._client

        if self.auth_type == CMRESHandler.AuthType.BASIC_AUTH:
            if self._client is None:
                return Elasticsearch(hosts=self.hosts,
                                     http_auth=self.auth_details,
                                     use_ssl=self.use_ssl,
                                     verify_certs=self.verify_certs,
                                     connection_class=RequestsHttpConnection,
                                     serializer=self.serializer,
                                     retry_on_timeout=self.es_retry_on_timeout,
                                     timeout=self.es_timeout,
                                     client_cert=self.client_cert,
                                     ca_certs=self.ca_certs)
            return self._client

        if self.auth_type == CMRESHandler.AuthType.KERBEROS_AUTH:
            if not CMR_KERBEROS_SUPPORTED:
                raise EnvironmentError("Kerberos module not available. Please install \"requests-kerberos\"")
            # For kerberos we return a new client each time to make sure the tokens are up to date
            return Elasticsearch(hosts=self.hosts,
                                 use_ssl=self.use_ssl,
                                 verify_certs=self.verify_certs,
                                 connection_class=RequestsHttpConnection,
                                 http_auth=HTTPKerberosAuth(mutual_authentication=DISABLED),
                                 serializer=self.serializer,
                                 retry_on_timeout=self.es_retry_on_timeout,
                                 timeout=self.es_timeout,
                                 client_cert=self.client_cert,
                                 ca_certs=self.ca_certs)

        if self.auth_type == CMRESHandler.AuthType.AWS_SIGNED_AUTH:
            if not AWS4AUTH_SUPPORTED:
                raise EnvironmentError("AWS4Auth not available. Please install \"requests-aws4auth\"")
            if self._client is None:
                awsauth = AWS4Auth(self.aws_access_key, self.aws_secret_key, self.aws_region, 'es')
                self._client = Elasticsearch(
                    hosts=self.hosts,
                    http_auth=awsauth,
                    use_ssl=self.use_ssl,
                    verify_certs=True,
                    connection_class=RequestsHttpConnection,
                    serializer=self.serializer,
                    retry_on_timeout=self.es_retry_on_timeout,
                    timeout=self.es_timeout,
                    client_cert=self.client_cert,
                    ca_certs=self.ca_certs)
            return self._client

        raise ValueError("Authentication method not supported")

    def test_es_source(self):
        """ Returns True if the handler can ping the Elasticsearch servers

        Can be used to confirm the setup of a handler has been properly done and confirm
        that things like the authentication is working properly

        :return: A boolean, True if the connection against elasticserach host was successful
        """
        return self.__get_es_client().ping()

    @staticmethod
    def __get_es_datetime_str(timestamp):
        """ Returns elasticsearch utc formatted time for an epoch timestamp

        :param timestamp: epoch, including milliseconds
        :return: A string valid for elasticsearch time record
        """
        current_date = datetime.datetime.utcfromtimestamp(timestamp)
        return "{0!s}.{1:03d}Z".format(current_date.strftime('%Y-%m-%dT%H:%M:%S'), int(current_date.microsecond / 1000))

    def flush(self):
        """ Flushes the buffer into ES
        :return: None
        """
        if self._timer is not None and self._timer.is_alive():
            self._timer.cancel()
        self._timer = None

        if self._buffer:
            try:
                with self._buffer_lock:
                    logs_buffer = self._buffer
                    self._buffer = []
                actions = (
                    {
                        '_index': self._index_name_func.__func__(self.es_index_name),
                        '_type': self.es_doc_type,
                        '_source': log_record
                    }
                    for log_record in logs_buffer
                )
                eshelpers.bulk(
                    client=self.__get_es_client(),
                    actions=actions,
                    stats_only=True
                )
            except Exception as exception:
                if self.raise_on_indexing_exceptions:
                    raise exception

    def close(self):
        """ Flushes the buffer and release any outstanding resource

        :return: None
        """
        if self._timer is not None:
            self.flush()
        self._timer = None

    def emit(self, record):
        """ Emit overrides the abstract logging.Handler logRecord emit method

        Format and records the log

        :param record: A class of type ```logging.LogRecord```
        :return: None
        """
        self.format(record)

        rec = self.es_additional_fields.copy()
        for key, value in record.__dict__.items():
            if key not in CMRESHandler.__LOGGING_FILTER_FIELDS:
                if key == "args":
                    value = tuple(str(arg) for arg in value)
                rec[key] = "" if value is None else value
        rec[self.default_timestamp_field_name] = self.__get_es_datetime_str(record.created)
        with self._buffer_lock:
            self._buffer.append(rec)

        if len(self._buffer) >= self.buffer_size:
            self.flush()
        else:
            self.__schedule_flush()


class NonBlockingCMRESHandler(CMRESHandler):
    """
    CMRESHandler multiprocessing implementation.

    Each bulk send is performed in a new process.
    Fork safe, thread safe.
    """

    def __init__(self, *args, **kwargs):
        """Overridde constructor creates threads list."""
        super(NonBlockingCMRESHandler, self).__init__(*args, **kwargs)
        self.bulk_send_processes = []
        self.original_pid = os.getpid()  # on handler init

    def flush(self):
        """Call eshelpers.bulk() in a new thread."""
        # if flush() will be called first after a fork,
        # self.bulk_send_processes may contain non-child process
        # it is required to try refresh_forked_handler()
        self.refresh_forked_handler()

        if self._timer is not None and self._timer.is_alive():
            self._timer.cancel()
        self._timer = None

        # join and delete dead bulk_send processes
        alive_processes = []
        for process in self.bulk_send_processes:
            if not process.is_alive():
                process.join()  # blocking w/o timeout
            else:
                alive_processes.append(process)
        self.bulk_send_processes = alive_processes

        if self._buffer:
            try:
                with self._buffer_lock:
                    logs_buffer = self._buffer
                    self._buffer = []
                actions = (
                    {
                        '_index':
                            self._index_name_func.__func__(self.es_index_name),
                        '_type': self.es_doc_type,
                        '_source': log_record
                    }
                    for log_record in logs_buffer
                )
                # call bulk send in a new process
                bulk_send_process = Process(
                    target=eshelpers.bulk,
                    kwargs={
                        # name mangling is used in get_es_client()
                        'client': self._CMRESHandler__get_es_client(),
                        'actions': actions,
                        'stats_only': True
                    }
                )
                bulk_send_process.name = \
                    "ES Logging {} @ {}".format(os.getpid(), time.time())
                bulk_send_process.start()
                # save reference to the child process
                self.bulk_send_processes.append(bulk_send_process)

            except Exception as exception:
                if self.raise_on_indexing_exceptions:
                    raise exception

    def close(self):
        """Wait for exisiting bulk_send process(es) before close."""
        for process in self.bulk_send_processes:
            if process.is_alive():
                process.join()  # blocking w/o timeout
        super(NonBlockingCMRESHandler, self).close()

    def refresh_forked_handler(self):
        """Wipe shared threading resources after fork."""
        current_pid = os.getpid()
        if self.original_pid != current_pid:  # is forked
            # check threading lock, release if needed
            if self._buffer_lock.locked():
                self._buffer_lock.release()
            # wipe buffer, the content will be send by a parent process
            with self._buffer_lock:
                self._buffer = []
            # wipe sending timer (refrence to the thread)
            self._timer = None
            # wipe child processes list
            self.bulk_send_processes = []
            # update PID
            self.original_pid = current_pid

    def emit(self, record):
        """
        Override emit function to add fork check.

        Some parent class methods using name mangling.
        """
        self.refresh_forked_handler()
        self.format(record)

        rec = self.es_additional_fields.copy()
        for key, value in record.__dict__.items():
            if key not in CMRESHandler._CMRESHandler__LOGGING_FILTER_FIELDS:
                if key == "args":
                    value = tuple(str(arg) for arg in value)
                rec[key] = "" if value is None else value
        rec[self.default_timestamp_field_name] = \
            self._CMRESHandler__get_es_datetime_str(record.created)
        with self._buffer_lock:
            self._buffer.append(rec)

        if len(self._buffer) >= self.buffer_size:
            self.flush()
        else:
            self._CMRESHandler__schedule_flush()
