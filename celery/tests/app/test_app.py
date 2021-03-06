from __future__ import absolute_import

import os

from mock import Mock, patch
from pickle import loads, dumps

from kombu import Exchange

from celery import Celery, shared_task, current_app
from celery import app as _app
from celery import _state
from celery.app import base as _appbase
from celery.app import defaults
from celery.exceptions import ImproperlyConfigured
from celery.five import items
from celery.loaders.base import BaseLoader
from celery.platforms import pyimplementation
from celery.utils.serialization import pickle

from celery.tests import config
from celery.tests.case import (
    Case,
    mask_modules,
    platform_pyimp,
    sys_platform,
    pypy_version,
)
from celery.utils import uuid
from celery.utils.mail import ErrorMail

THIS_IS_A_KEY = 'this is a value'


class ObjectConfig(object):
    FOO = 1
    BAR = 2

object_config = ObjectConfig()
dict_config = dict(FOO=10, BAR=20)


class Object(object):

    def __init__(self, **kwargs):
        for key, value in items(kwargs):
            setattr(self, key, value)


def _get_test_config():
    return dict((key, getattr(config, key))
                for key in dir(config)
                if key.isupper() and not key.startswith('_'))

test_config = _get_test_config()


class test_module(Case):

    def test_default_app(self):
        self.assertEqual(_app.default_app, _state.default_app)

    def test_bugreport(self):
        self.assertTrue(_app.bugreport())


class test_App(Case):

    def setUp(self):
        self.app = Celery(set_as_current=False)
        self.app.conf.update(test_config)

    def test_task(self):
        app = Celery('foozibari', set_as_current=False)

        def fun():
            pass

        fun.__module__ = '__main__'
        task = app.task(fun)
        self.assertEqual(task.name, app.main + '.fun')

    def test_with_config_source(self):
        app = Celery(set_as_current=False, config_source=ObjectConfig)
        self.assertEqual(app.conf.FOO, 1)
        self.assertEqual(app.conf.BAR, 2)

    def test_task_windows_execv(self):
        app = Celery(set_as_current=False)

        prev, _appbase._EXECV = _appbase._EXECV, True
        try:

            @app.task()
            def foo():
                pass

            self.assertTrue(foo._get_current_object())  # is proxy

        finally:
            _appbase._EXECV = prev
        assert not _appbase._EXECV

    def test_task_takes_no_args(self):
        app = Celery(set_as_current=False)

        with self.assertRaises(TypeError):
            @app.task(1)
            def foo():
                pass

    def test_add_defaults(self):
        app = Celery(set_as_current=False)

        self.assertFalse(app.configured)
        _conf = {'FOO': 300}
        conf = lambda: _conf
        app.add_defaults(conf)
        self.assertIn(conf, app._pending_defaults)
        self.assertFalse(app.configured)
        self.assertEqual(app.conf.FOO, 300)
        self.assertTrue(app.configured)
        self.assertFalse(app._pending_defaults)

        # defaults not pickled
        appr = loads(dumps(app))
        with self.assertRaises(AttributeError):
            appr.conf.FOO

        # add more defaults after configured
        conf2 = {'FOO': 'BAR'}
        app.add_defaults(conf2)
        self.assertEqual(app.conf.FOO, 'BAR')

        self.assertIn(_conf, app.conf.defaults)
        self.assertIn(conf2, app.conf.defaults)

    def test_connection_or_acquire(self):

        with self.app.connection_or_acquire(block=True):
            self.assertTrue(self.app.pool._dirty)

        with self.app.connection_or_acquire(pool=False):
            self.assertFalse(self.app.pool._dirty)

    def test_maybe_close_pool(self):
        cpool = self.app._pool = Mock()
        ppool = self.app.amqp._producer_pool = Mock()
        self.app._maybe_close_pool()
        cpool.force_close_all.assert_called_with()
        ppool.force_close_all.assert_called_with()
        self.assertIsNone(self.app._pool)
        self.assertIsNone(self.app.amqp._producer_pool)

        self.app._pool = Mock()
        self.app._maybe_close_pool()
        self.app._maybe_close_pool()

    def test_using_v1_reduce(self):
        self.app._using_v1_reduce = True
        self.assertTrue(loads(dumps(self.app)))

    def test_autodiscover_tasks(self):
        self.app.conf.CELERY_FORCE_BILLIARD_LOGGING = True
        with patch('celery.app.base.ensure_process_aware_logger') as ep:
            self.app.loader.autodiscover_tasks = Mock()
            self.app.autodiscover_tasks(['proj.A', 'proj.B'])
            ep.assert_called_with()
            self.app.loader.autodiscover_tasks.assert_called_with(
                ['proj.A', 'proj.B'], 'tasks',
            )
        with patch('celery.app.base.ensure_process_aware_logger') as ep:
            self.app.conf.CELERY_FORCE_BILLIARD_LOGGING = False
            self.app.autodiscover_tasks(['proj.A', 'proj.B'])
            self.assertFalse(ep.called)

    def test_with_broker(self):
        prev = os.environ.get('CELERY_BROKER_URL')
        os.environ.pop('CELERY_BROKER_URL', None)
        try:
            app = Celery(set_as_current=False, broker='foo://baribaz')
            self.assertEqual(app.conf.BROKER_HOST, 'foo://baribaz')
        finally:
            os.environ['CELERY_BROKER_URL'] = prev

    def test_repr(self):
        self.assertTrue(repr(self.app))

    def test_custom_task_registry(self):
        app1 = Celery(set_as_current=False)
        app2 = Celery(set_as_current=False, tasks=app1.tasks)
        self.assertIs(app2.tasks, app1.tasks)

    def test_include_argument(self):
        app = Celery(set_as_current=False, include=('foo', 'bar.foo'))
        self.assertEqual(app.conf.CELERY_IMPORTS, ('foo', 'bar.foo'))

    def test_set_as_current(self):
        current = _state._tls.current_app
        try:
            app = Celery(set_as_current=True)
            self.assertIs(_state._tls.current_app, app)
        finally:
            _state._tls.current_app = current

    def test_current_task(self):
        app = Celery(set_as_current=False)

        @app.task
        def foo():
            pass

        _state._task_stack.push(foo)
        try:
            self.assertEqual(app.current_task.name, foo.name)
        finally:
            _state._task_stack.pop()

    def test_task_not_shared(self):
        with patch('celery.app.base.shared_task') as sh:
            app = Celery(set_as_current=False)

            @app.task(shared=False)
            def foo():
                pass
            self.assertFalse(sh.called)

    def test_task_compat_with_filter(self):
        app = Celery(set_as_current=False, accept_magic_kwargs=True)
        check = Mock()

        def filter(task):
            check(task)
            return task

        @app.task(filter=filter)
        def foo():
            pass
        check.assert_called_with(foo)

    def test_task_with_filter(self):
        app = Celery(set_as_current=False, accept_magic_kwargs=False)
        check = Mock()

        def filter(task):
            check(task)
            return task

        assert not _appbase._EXECV

        @app.task(filter=filter)
        def foo():
            pass
        check.assert_called_with(foo)

    def test_task_sets_main_name_MP_MAIN_FILE(self):
        from celery import utils as _utils
        _utils.MP_MAIN_FILE = __file__
        try:
            app = Celery('xuzzy', set_as_current=False)

            @app.task
            def foo():
                pass

            self.assertEqual(foo.name, 'xuzzy.foo')
        finally:
            _utils.MP_MAIN_FILE = None

    def test_base_task_inherits_magic_kwargs_from_app(self):
        from celery.task import Task as OldTask

        class timkX(OldTask):
            abstract = True

        app = Celery(set_as_current=False, accept_magic_kwargs=True)
        timkX.bind(app)
        # see #918
        self.assertFalse(timkX.accept_magic_kwargs)

        from celery import Task as NewTask

        class timkY(NewTask):
            abstract = True

        timkY.bind(app)
        self.assertFalse(timkY.accept_magic_kwargs)

    def test_annotate_decorator(self):
        from celery.app.task import Task

        class adX(Task):
            abstract = True

            def run(self, y, z, x):
                return y, z, x

        check = Mock()

        def deco(fun):

            def _inner(*args, **kwargs):
                check(*args, **kwargs)
                return fun(*args, **kwargs)
            return _inner

        app = Celery(set_as_current=False)
        app.conf.CELERY_ANNOTATIONS = {
            adX.name: {'@__call__': deco}
        }
        adX.bind(app)
        self.assertIs(adX.app, app)

        i = adX()
        i(2, 4, x=3)
        check.assert_called_with(i, 2, 4, x=3)

        i.annotate()
        i.annotate()

    def test_apply_async_has__self__(self):
        app = Celery(set_as_current=False)

        @app.task(__self__='hello')
        def aawsX():
            pass

        with patch('celery.app.amqp.TaskProducer.publish_task') as dt:
            aawsX.apply_async((4, 5))
            args = dt.call_args[0][1]
            self.assertEqual(args, ('hello', 4, 5))

    def test_apply_async__connection_arg(self):
        app = Celery(set_as_current=False)

        @app.task()
        def aacaX():
            pass

        connection = app.connection('asd://')
        with self.assertRaises(KeyError):
            aacaX.apply_async(connection=connection)

    def test_apply_async_adds_children(self):
        from celery._state import _task_stack
        app = Celery(set_as_current=False)

        @app.task()
        def a3cX1(self):
            pass

        @app.task()
        def a3cX2(self):
            pass

        _task_stack.push(a3cX1)
        try:
            a3cX1.push_request(called_directly=False)
            try:
                res = a3cX2.apply_async(add_to_parent=True)
                self.assertIn(res, a3cX1.request.children)
            finally:
                a3cX1.pop_request()
        finally:
            _task_stack.pop()

    def test_TaskSet(self):
        ts = self.app.TaskSet()
        self.assertListEqual(ts.tasks, [])
        self.assertIs(ts.app, self.app)

    def test_pickle_app(self):
        changes = dict(THE_FOO_BAR='bars',
                       THE_MII_MAR='jars')
        self.app.conf.update(changes)
        saved = pickle.dumps(self.app)
        self.assertLess(len(saved), 2048)
        restored = pickle.loads(saved)
        self.assertDictContainsSubset(changes, restored.conf)

    def test_worker_main(self):
        from celery.bin import worker as worker_bin

        class worker(worker_bin.worker):

            def execute_from_commandline(self, argv):
                return argv

        prev, worker_bin.worker = worker_bin.worker, worker
        try:
            ret = self.app.worker_main(argv=['--version'])
            self.assertListEqual(ret, ['--version'])
        finally:
            worker_bin.worker = prev

    def test_config_from_envvar(self):
        os.environ['CELERYTEST_CONFIG_OBJECT'] = 'celery.tests.app.test_app'
        self.app.config_from_envvar('CELERYTEST_CONFIG_OBJECT')
        self.assertEqual(self.app.conf.THIS_IS_A_KEY, 'this is a value')

    def test_config_from_object(self):

        class Object(object):
            LEAVE_FOR_WORK = True
            MOMENT_TO_STOP = True
            CALL_ME_BACK = 123456789
            WANT_ME_TO = False
            UNDERSTAND_ME = True

        self.app.config_from_object(Object())

        self.assertTrue(self.app.conf.LEAVE_FOR_WORK)
        self.assertTrue(self.app.conf.MOMENT_TO_STOP)
        self.assertEqual(self.app.conf.CALL_ME_BACK, 123456789)
        self.assertFalse(self.app.conf.WANT_ME_TO)
        self.assertTrue(self.app.conf.UNDERSTAND_ME)

    def test_config_from_cmdline(self):
        cmdline = ['.always_eager=no',
                   '.result_backend=/dev/null',
                   'celeryd.prefetch_multiplier=368',
                   '.foobarstring=(string)300',
                   '.foobarint=(int)300',
                   '.result_engine_options=(dict){"foo": "bar"}']
        self.app.config_from_cmdline(cmdline, namespace='celery')
        self.assertFalse(self.app.conf.CELERY_ALWAYS_EAGER)
        self.assertEqual(self.app.conf.CELERY_RESULT_BACKEND, '/dev/null')
        self.assertEqual(self.app.conf.CELERYD_PREFETCH_MULTIPLIER, 368)
        self.assertEqual(self.app.conf.CELERY_FOOBARSTRING, '300')
        self.assertEqual(self.app.conf.CELERY_FOOBARINT, 300)
        self.assertDictEqual(self.app.conf.CELERY_RESULT_ENGINE_OPTIONS,
                             {'foo': 'bar'})

    def test_compat_setting_CELERY_BACKEND(self):

        self.app.config_from_object(Object(CELERY_BACKEND='set_by_us'))
        self.assertEqual(self.app.conf.CELERY_RESULT_BACKEND, 'set_by_us')

    def test_setting_BROKER_TRANSPORT_OPTIONS(self):

        _args = {'foo': 'bar', 'spam': 'baz'}

        self.app.config_from_object(Object())
        self.assertEqual(self.app.conf.BROKER_TRANSPORT_OPTIONS, {})

        self.app.config_from_object(Object(BROKER_TRANSPORT_OPTIONS=_args))
        self.assertEqual(self.app.conf.BROKER_TRANSPORT_OPTIONS, _args)

    def test_Windows_log_color_disabled(self):
        self.app.IS_WINDOWS = True
        self.assertFalse(self.app.log.supports_color(True))

    def test_compat_setting_CARROT_BACKEND(self):
        self.app.config_from_object(Object(CARROT_BACKEND='set_by_us'))
        self.assertEqual(self.app.conf.BROKER_TRANSPORT, 'set_by_us')

    def test_WorkController(self):
        x = self.app.WorkController
        self.assertIs(x.app, self.app)

    def test_Worker(self):
        x = self.app.Worker
        self.assertIs(x.app, self.app)

    def test_AsyncResult(self):
        x = self.app.AsyncResult('1')
        self.assertIs(x.app, self.app)
        r = loads(dumps(x))
        # not set as current, so ends up as default app after reduce
        self.assertIs(r.app, _state.default_app)

    def test_get_active_apps(self):
        self.assertTrue(list(_state._get_active_apps()))

        app1 = Celery(set_as_current=False)
        appid = id(app1)
        self.assertIn(app1, _state._get_active_apps())
        del(app1)

        # weakref removed from list when app goes out of scope.
        with self.assertRaises(StopIteration):
            next(app for app in _state._get_active_apps() if id(app) == appid)

    def test_config_from_envvar_more(self, key='CELERY_HARNESS_CFG1'):
        self.assertFalse(self.app.config_from_envvar('HDSAJIHWIQHEWQU',
                                                     silent=True))
        with self.assertRaises(ImproperlyConfigured):
            self.app.config_from_envvar('HDSAJIHWIQHEWQU', silent=False)
        os.environ[key] = __name__ + '.object_config'
        self.assertTrue(self.app.config_from_envvar(key))
        self.assertEqual(self.app.conf['FOO'], 1)
        self.assertEqual(self.app.conf['BAR'], 2)

        os.environ[key] = 'unknown_asdwqe.asdwqewqe'
        with self.assertRaises(ImportError):
            self.app.config_from_envvar(key, silent=False)
        self.assertFalse(self.app.config_from_envvar(key, silent=True))

        os.environ[key] = __name__ + '.dict_config'
        self.assertTrue(self.app.config_from_envvar(key))
        self.assertEqual(self.app.conf['FOO'], 10)
        self.assertEqual(self.app.conf['BAR'], 20)

    @patch('celery.bin.celery.CeleryCommand.execute_from_commandline')
    def test_start(self, execute):
        self.app.start()
        self.assertTrue(execute.called)

    def test_mail_admins(self):

        class Loader(BaseLoader):

            def mail_admins(*args, **kwargs):
                return args, kwargs

        self.app.loader = Loader(app=self.app)
        self.app.conf.ADMINS = None
        self.assertFalse(self.app.mail_admins('Subject', 'Body'))
        self.app.conf.ADMINS = [('George Costanza', 'george@vandelay.com')]
        self.assertTrue(self.app.mail_admins('Subject', 'Body'))

    def test_amqp_get_broker_info(self):
        self.assertDictContainsSubset(
            {'hostname': 'localhost',
             'userid': 'guest',
             'password': 'guest',
             'virtual_host': '/'},
            self.app.connection('pyamqp://').info(),
        )
        self.app.conf.BROKER_PORT = 1978
        self.app.conf.BROKER_VHOST = 'foo'
        self.assertDictContainsSubset(
            {'port': 1978, 'virtual_host': 'foo'},
            self.app.connection('pyamqp://:1978/foo').info(),
        )
        conn = self.app.connection('pyamqp:////value')
        self.assertDictContainsSubset({'virtual_host': '/value'},
                                      conn.info())

    def test_BROKER_BACKEND_alias(self):
        self.assertEqual(self.app.conf.BROKER_BACKEND,
                         self.app.conf.BROKER_TRANSPORT)

    def test_after_fork(self):
        p = self.app._pool = Mock()
        self.app._after_fork(self.app)
        p.force_close_all.assert_called_with()
        self.assertIsNone(self.app._pool)
        self.app._after_fork(self.app)

    def test_pool_no_multiprocessing(self):
        with mask_modules('multiprocessing.util'):
            pool = self.app.pool
            self.assertIs(pool, self.app._pool)

    def test_bugreport(self):
        self.assertTrue(self.app.bugreport())

    def test_send_task_sent_event(self):

        class Dispatcher(object):
            sent = []

            def publish(self, type, fields, *args, **kwargs):
                self.sent.append((type, fields))

        conn = self.app.connection()
        chan = conn.channel()
        try:
            for e in ('foo_exchange', 'moo_exchange', 'bar_exchange'):
                chan.exchange_declare(e, 'direct', durable=True)
                chan.queue_declare(e, durable=True)
                chan.queue_bind(e, e, e)
        finally:
            chan.close()
        assert conn.transport_cls == 'memory'

        prod = self.app.amqp.TaskProducer(
            conn, exchange=Exchange('foo_exchange'),
            send_sent_event=True,
        )

        dispatcher = Dispatcher()
        self.assertTrue(prod.publish_task('footask', (), {},
                                          exchange='moo_exchange',
                                          routing_key='moo_exchange',
                                          event_dispatcher=dispatcher))
        self.assertTrue(dispatcher.sent)
        self.assertEqual(dispatcher.sent[0][0], 'task-sent')
        self.assertTrue(prod.publish_task('footask', (), {},
                                          event_dispatcher=dispatcher,
                                          exchange='bar_exchange',
                                          routing_key='bar_exchange'))

    def test_error_mail_sender(self):
        x = ErrorMail.subject % {'name': 'task_name',
                                 'id': uuid(),
                                 'exc': 'FOOBARBAZ',
                                 'hostname': 'lana'}
        self.assertTrue(x)

    def test_error_mail_disabled(self):
        task = Mock()
        x = ErrorMail(task)
        x.should_send = Mock()
        x.should_send.return_value = False
        x.send(Mock(), Mock())
        self.assertFalse(task.app.mail_admins.called)


class test_defaults(Case):

    def test_str_to_bool(self):
        for s in ('false', 'no', '0'):
            self.assertFalse(defaults.strtobool(s))
        for s in ('true', 'yes', '1'):
            self.assertTrue(defaults.strtobool(s))
        with self.assertRaises(TypeError):
            defaults.strtobool('unsure')


class test_debugging_utils(Case):

    def test_enable_disable_trace(self):
        try:
            _app.enable_trace()
            self.assertEqual(_app.app_or_default, _app._app_or_default_trace)
            _app.disable_trace()
            self.assertEqual(_app.app_or_default, _app._app_or_default)
        finally:
            _app.disable_trace()


class test_pyimplementation(Case):

    def test_platform_python_implementation(self):
        with platform_pyimp(lambda: 'Xython'):
            self.assertEqual(pyimplementation(), 'Xython')

    def test_platform_jython(self):
        with platform_pyimp():
            with sys_platform('java 1.6.51'):
                self.assertIn('Jython', pyimplementation())

    def test_platform_pypy(self):
        with platform_pyimp():
            with sys_platform('darwin'):
                with pypy_version((1, 4, 3)):
                    self.assertIn('PyPy', pyimplementation())
                with pypy_version((1, 4, 3, 'a4')):
                    self.assertIn('PyPy', pyimplementation())

    def test_platform_fallback(self):
        with platform_pyimp():
            with sys_platform('darwin'):
                with pypy_version():
                    self.assertEqual('CPython', pyimplementation())


class test_shared_task(Case):

    def setUp(self):
        self._restore_app = current_app._get_current_object()

    def tearDown(self):
        self._restore_app.set_current()

    def test_registers_to_all_apps(self):
        xproj = Celery('xproj')
        xproj.finalize()

        @shared_task
        def foo():
            return 42

        @shared_task()
        def bar():
            return 84

        self.assertIs(foo.app, xproj)
        self.assertIs(bar.app, xproj)
        self.assertTrue(foo._get_current_object())

        yproj = Celery('yproj')
        self.assertIs(foo.app, yproj)
        self.assertIs(bar.app, yproj)

        @shared_task()
        def baz():
            return 168

        self.assertIs(baz.app, yproj)
