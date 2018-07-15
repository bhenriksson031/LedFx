from asyncio import coroutines, ensure_future
import concurrent.futures
import voluptuous as vol
from abc import ABC
import threading
import logging
import inspect
import importlib
import pkgutil

_LOGGER = logging.getLogger(__name__)

def async_fire_and_forget(coro, loop):
    """Run some code in the core event loop without a result"""

    if not coroutines.iscoroutine(coro):
        raise TypeError(('A coroutine object is required: {}').format(coro))

    def callback():
        """Handle the firing of a coroutine."""
        ensure_future(coro, loop=loop)

    loop.call_soon_threadsafe(callback)
    return

def async_callback(loop, callback, *args):
    """Run a callback in the event loop with access to the result"""

    future = concurrent.futures.Future()
    def run_callback():
        try:
            future.set_result(callback(*args))
        # pylint: disable=broad-except
        except Exception as e:
            if future.set_running_or_notify_cancel():
                future.set_exception(e)
            else:
                _LOGGER.warning("Exception on lost future: ", exc_info=True)

    loop.call_soon_threadsafe(run_callback)
    return future

def hasattr_explicit(cls, attr):
    """Returns if the given object has explicitly declared an attribute"""
    try:
        return getattr(cls, attr) != getattr(super(cls, cls), attr, None)
    except AttributeError:
        return False

def getattr_explicit(cls, attr, *default):
    """Gets an explicit attribute from an object"""

    if len(default) > 1:
        raise TypeError("getattr_explicit expected at most 3 arguments, got {}".format(
            len(default) + 2))

    if hasattr_explicit(cls, attr):
        return getattr(cls, attr, default)
    if default:
        return default[0]

    raise AttributeError("type object '{}' has no attribute '{}'.".format(
        cls.__name__, attr))

class BaseRegistry(ABC):
    """
    Base registry class used for effects and devices. This maintains a
    list of automatically registered base classes and assembles schema
    information

    The prevent registration for classes that are intended to serve as 
    base classes (i.e. GradientEffect) add the following declarator:
        @Effect.no_registration
    """
    _schema_attr = 'CONFIG_SCHEMA'

    def __init_subclass__(cls, **kwargs):
        """Automatically register the class"""
        super().__init_subclass__(**kwargs)

        if not hasattr(cls, '_registry'):
            cls._registry = {}

        name = cls.__module__.split('.')[-1]
        cls._registry[name] = cls

    @classmethod
    def no_registration(self, cls):
        """Clear registration entiry based on special declarator"""

        name = cls.__module__.split('.')[-1]
        del cls._registry[name]
        return cls

    @classmethod
    def schema(self, extended=True, extra=vol.ALLOW_EXTRA):
        """Returns the extended schema of the class"""

        if extended is False:
            return getattr_explicit(type(self), self._schema_attr, vol.Schema({}))

        schema = vol.Schema({}, extra=extra)
        classes = inspect.getmro(self)[::-1]
        for c in classes:
            c_schema = getattr_explicit(c, self._schema_attr, None)
            if c_schema is not None:
                schema = schema.extend(c_schema.schema)

        return schema

    @classmethod
    def registry(self):
        """Returns all the subclasses in the registry"""

        return self._registry

class RegistryLoader(object):
    """Manages loading of compoents for a given registry"""

    def __init__(self, cls, package, ledfx):
        self._package = package
        self._ledfx = ledfx
        self._cls = cls
        self._objects = {}
        self._object_id = 1

        self.import_registry(package)

    def import_registry(self, package):
        """
        Imports all the modules in the package thus hydrating
        the registry for the class
        """

        found = self.discover_modules(package)
        _LOGGER.info("Importing {} from {}".format(found, package))
        for name in found:
            importlib.import_module(name)

    def discover_modules(self, package):
        """Discovers all modules in the package"""
        module = importlib.import_module(package)
        
        found = []
        for _, name, _ in pkgutil.iter_modules(module.__path__, package + '.'):
            found.append(name)
        
        return found

    def __iter__(self):
        return iter(self._objects)

    def classes(self):
        """Returns all the classes in the regsitry"""
        return self._cls.registry()

    def values(self):
        """Returns all the created objects"""
        return self._objects.values()

    def reload(self, force = False):
        """Reloads the registry"""

        # TODO: Deteremine exactly how to reload. This seems to work sometimes
        # depending on the current state. Probably need to invalidate the
        # system cash to ensure everything gets reloaded
        self.import_registry(self._package)

    def create(self, name, config = {}, id = None, *args):
        """Loads and creates a object from the registry by name"""

        if name not in self._cls.registry():
            raise AttributeError(("Couldn't find '{}' in the {} registry").format(
                name, self._cls.__name__.lower()))
        if id is None:
            id = self._object_id
            self._object_id = self._object_id + 1
        if id in self._objects:
            raise AttributeError(("Object with id '{}' already created").format(id))

        # Create the new object based on the registry entires and 
        # validate the schema.
        _cls = self._cls.registry().get(name)
        if config is not None:
            config = _cls.schema()(config)
            obj =  _cls(config, *args)
        else:
            obj =  _cls(*args)

        # Store the object into the internal list and return it
        self._objects[id] = obj
        return obj

    def destroy(self, id):

        if id not in self._objects:
            raise AttributeError(("Object with id '{}' does not exist.").format(id))
        del self._objects[id]