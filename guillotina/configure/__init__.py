from collections import OrderedDict
from guillotina.configure import component
from guillotina.configure.behaviors import BehaviorAdapterFactory
from guillotina.configure.behaviors import BehaviorRegistration
from guillotina.exceptions import ConfigurationError
from guillotina.interfaces import DEFAULT_ADD_PERMISSION
from guillotina.interfaces import IBehavior
from guillotina.interfaces import IBehaviorSchemaAwareFactory
from guillotina.interfaces import IDefaultLayer
from guillotina.interfaces import IPermission
from guillotina.interfaces import IResourceFactory
from guillotina.interfaces import IRole
from guillotina.security.permission import Permission
from guillotina.utils import get_caller_module
from guillotina.utils import get_module_dotted_name
from guillotina.utils import resolve_dotted_name
from guillotina.utils import resolve_module_path
from zope.interface import classImplements
from zope.interface import Interface

import logging


_registered_configurations = []
# stored as tuple of (type, configuration) so we get keep it in the order
# it is registered even if you mix types of registrations

_registered_configuration_handlers = {}

logger = logging.getLogger('guillotina')


def get_configurations(module_name, type_=None):
    results = []
    for reg_type, registration in _registered_configurations:
        if type_ is not None and reg_type != type_:
            continue
        config = registration['config']
        module = config.get('module', registration.get('klass'))
        if (get_module_dotted_name(
                resolve_dotted_name(module)) + '.').startswith(module_name + '.'):
            results.append((reg_type, registration))
    return results


def register_configuration_handler(type_, handler):
    _registered_configuration_handlers[type_] = handler


def register_configuration(klass, config, type_):
    _registered_configurations.append((type_, {
        'klass': klass,
        'config': config
    }))


def load_configuration(_context, module_name, _type):
    if _type not in _registered_configuration_handlers:
        raise Exception('Configuration handler for {} not registered'.format(_type))
    for _type, configuration in get_configurations(module_name, _type):
        _registered_configuration_handlers[_type](_context, configuration)


def load_all_configurations(_context, module_name):
    for type_, configuration in get_configurations(module_name):
        try:
            _registered_configuration_handlers[type_](_context, configuration)
        except TypeError as e:
            logger.error('Can not find %s module' % configuration)
            raise


def load_service(_context, service):
    # prevent circular import
    from guillotina import app_settings
    from guillotina.security.utils import protect_view

    service_conf = service['config']
    factory = resolve_dotted_name(service['klass'])

    permission = service_conf.get(
        'permission', app_settings.get('default_permission', None))

    protect_view(factory, permission)

    method = service_conf.get('method', 'GET')
    default_layer = resolve_dotted_name(
        app_settings.get('default_layer', IDefaultLayer))
    layer = service_conf.get('layer', default_layer)
    name = service_conf.get('name', '')
    content = service_conf.get('context', Interface)
    logger.debug('Defining adapter for '  # noqa
                 '{0:s} {1:s} {2:s} to {3:s} name {4:s}'.format(
        content.__identifier__,
        app_settings['http_methods'][method].__identifier__,
        layer.__identifier__,
        str(factory),
        name))
    component.adapter(
        _context,
        factory=(factory,),
        provides=app_settings['http_methods'][method],
        for_=(content, layer),
        name=name
    )

    api = app_settings['api_definition']
    ct_name = content.__identifier__
    if ct_name not in api:
        api[ct_name] = OrderedDict()
    ct_api = api[ct_name]
    if name:
        if 'endpoints' not in ct_api:
            ct_api['endpoints'] = OrderedDict()
        if name not in ct_api['endpoints']:
            ct_api['endpoints'][name] = OrderedDict()
        ct_api['endpoints'][name][method] = OrderedDict(service_conf)
    else:
        ct_api[method] = OrderedDict(service_conf)
register_configuration_handler('service', load_service)  # noqa


def load_contenttype(_context, contenttype):
    conf = contenttype['config']
    klass = contenttype['klass']
    if 'schema' in conf:
        classImplements(klass, conf['schema'])

    from guillotina.content import ResourceFactory

    factory = ResourceFactory(
        klass,
        title='',
        description='',
        type_name=conf['type_name'],
        schema=resolve_dotted_name(conf.get('schema', Interface)),
        behaviors=[resolve_dotted_name(b) for b in conf.get('behaviors', []) or ()],
        add_permission=conf.get('add_permission') or DEFAULT_ADD_PERMISSION,
        allowed_types=conf.get('allowed_types', None)
    )
    component.utility(
        _context,
        provides=IResourceFactory,
        component=factory,
        name=conf['type_name'],
    )
register_configuration_handler('contenttype', load_contenttype)  # noqa


def load_behavior(_context, behavior):
    conf = behavior['config']
    klass = resolve_dotted_name(behavior['klass'])
    factory = conf.get('factory') or klass
    real_factory = resolve_dotted_name(factory)
    schema = resolve_dotted_name(conf['provides'])
    classImplements(real_factory, schema)

    name = conf.get('name')
    name_only = conf.get('name_only', False)
    title = conf.get('title', '')
    for_ = resolve_dotted_name(conf.get('for_'))
    marker = resolve_dotted_name(conf.get('marker'))

    if marker is None and real_factory is None:
        marker = schema

    if marker is not None and real_factory is None and marker is not schema:
        raise ConfigurationError(
            u"You cannot specify a different 'marker' and 'provides' if "
            u"there is no adapter factory for the provided interface."
        )
    if name_only and name is None:
        raise ConfigurationError(
            u"If you decide to only register by 'name', a name must be given."
        )

    # Instantiate the real factory if it's the schema-aware type. We do
    # this here so that the for_ interface may take this into account.
    if factory is not None and IBehaviorSchemaAwareFactory.providedBy(factory):
        factory = factory(schema)

    registration = BehaviorRegistration(
        title=conf.get('title', ''),
        description=conf.get('description', ''),
        interface=schema,
        marker=marker,
        factory=real_factory,
        name=name,
        for_=for_
    )
    if not name_only:
        # behavior registration by provides interface identifier
        component.utility(
            _context,
            provides=IBehavior,
            name=schema.__identifier__,
            component=registration
        )

    if name is not None:
        # for convinience we register with a given name
        component.utility(
            _context,
            provides=IBehavior,
            name=name,
            component=registration
        )

    if factory is None:
        if for_ is not None:
            logger.warning(
                u"Specifying 'for' in behavior '{0}' if no 'factory' is given "
                u"has no effect and is superfluous.".format(title)
            )
        # w/o factory we're done here
        return

    if for_ is None:
        # Attempt to guess the factory's adapted interface and use it as
        # the 'for_'.
        # Fallback to '*' (=Interface).
        adapts = getattr(factory, '__component_adapts__', None) or [Interface]
        if len(adapts) != 1:
            raise ConfigurationError(
                u"The factory can not be declared as multi-adapter."
            )
        for_ = adapts[0]

    adapter_factory = BehaviorAdapterFactory(registration)

    component.adapter(
        _context,
        factory=(adapter_factory,),
        provides=schema,
        for_=(for_,)
    )
register_configuration_handler('behavior', load_behavior)  # noqa


def load_addon(_context, addon):
    from guillotina import app_settings
    config = addon['config']
    app_settings['available_addons'][config['name']] = {
        'title': config['title'],
        'handler': addon['klass']
    }
register_configuration_handler('addon', load_addon)  # noqa


def _component_conf(conf):
    if type(conf['for_']) not in (tuple, set, list):
        conf['for_'] = (conf['for_'],)


def load_adapter(_context, adapter):
    conf = adapter['config']
    klass = resolve_dotted_name(adapter['klass'])
    factory = conf.pop('factory', None) or klass
    _component_conf(conf)
    if 'provides' in conf and isinstance(klass, type):
        # not sure if this is what we want or not for sure but
        # we are automatically applying the provides interface to
        # registered class objects
        classImplements(klass, conf['provides'])
    component.adapter(
        _context,
        factory=(factory,),
        **conf
    )
register_configuration_handler('adapter', load_adapter)  # noqa


def load_subscriber(_context, subscriber):
    conf = subscriber['config']
    conf['handler'] = resolve_dotted_name(conf.get('handler') or subscriber['klass'])
    _component_conf(conf)
    component.subscriber(
        _context,
        **conf
    )
register_configuration_handler('subscriber', load_subscriber)  # noqa


def load_utility(_context, _utility):
    conf = _utility['config']
    if 'factory' in conf:
        conf['factory'] = resolve_dotted_name(conf['factory'])
    elif 'component' in conf:
        conf['component'] = resolve_dotted_name(conf['component'])
    else:
        # use provided klass
        klass = _utility['klass']
        if isinstance(klass, type):
            # is a class type, use factory setting
            conf['factory'] = klass
        else:
            # not a factory
            conf['component'] = klass
    component.utility(
        _context,
        **conf
    )
register_configuration_handler('utility', load_utility)  # noqa


def load_permission(_context, permission_conf):
    permission = Permission(**permission_conf['config'])
    component.utility(_context, IPermission, permission,
                      name=permission_conf['config']['id'])
register_configuration_handler('permission', load_permission)  # noqa


def load_role(_context, role):
    defineRole_directive(_context, **role['config'])
register_configuration_handler('role', load_role)  # noqa


def load_grant(_context, grant):
    grant_directive(_context, **grant['config'])
register_configuration_handler('grant', load_grant)  # noqa


def load_grant_all(_context, grant_all):
    grantAll_directive(_context, **grant_all['config'])
register_configuration_handler('grant_all', load_grant_all)  # noqa


def load_json_schema_definition(_context, json_schema):
    from guillotina import app_settings
    config = json_schema['config']
    app_settings['json_schema_definitions'][config['name']] = config['schema']
register_configuration_handler('json_schema_definition', load_json_schema_definition)  # noqa


class _base_decorator(object):
    configuration_type = ''

    def __init__(self, **config):
        self.config = config

    def __call__(self, klass):
        register_configuration(klass, self.config, self.configuration_type)
        return klass


class _factory_decorator(_base_decorator):
    """
    behavior that can pass factory to it so it can be used standalone
    """

    def __call__(self, klass=None):
        if klass is None:
            if 'factory' not in self.config:
                raise Exception('Must provide factory configuration when defining '
                                'without a class')
            klass = get_caller_module()
        return super(_factory_decorator, self).__call__(klass)


class service(_base_decorator):
    def __call__(self, func):
        if isinstance(func, type):
            # it is a class view, we don't need to generate one for it...
            register_configuration(func, self.config, 'service')
            if 'allow_access' in self.config:
                func.__allow_access__ = self.config['allow_access']
        else:
            # avoid circular imports
            from guillotina.api.service import Service

            class _View(self.config.get('base', Service)):
                __allow_access__ = self.config.get('allow_access', False)
                view_func = staticmethod(func)

                async def __call__(self):
                    return await func(self.context, self.request)

            self.config['module'] = func
            register_configuration(_View, self.config, 'service')
        return func


class contenttype(_base_decorator):
    configuration_type = 'contenttype'


class behavior(_factory_decorator):
    configuration_type = 'behavior'


class addon(_base_decorator):
    configuration_type = 'addon'


class adapter(_factory_decorator):
    configuration_type = 'adapter'


class subscriber(_factory_decorator):
    configuration_type = 'subscriber'


class utility(_factory_decorator):
    configuration_type = 'utility'


def permission(id, title, description=''):
    register_configuration(
        get_caller_module(),
        dict(
            id=id,
            title=title,
            description=description),
        'permission')


def role(id, title, description='', local=True):
    register_configuration(
        get_caller_module(),
        dict(
            id=id,
            title=title,
            description=description,
            local=local),
        'role')


def grant(principal=None, role=None, permission=None,
          permissions=None):
    register_configuration(
        get_caller_module(),
        dict(
            principal=principal,
            role=role,
            permission=permission,
            permissions=permissions),
        'grant')


def grant_all(principal=None, role=None):
    register_configuration(
        get_caller_module(),
        dict(
            principal=principal,
            role=role),
        'grant_all')


def json_schema_definition(name, schema):
    register_configuration(
        get_caller_module(),
        dict(name=name, schema=schema),
        'json_schema_definition')


def grant_directive(
        _context, principal=None, role=None, permission=None,
        permissions=None):
    from guillotina.security.security_code import role_permission_manager as role_perm_mgr
    from guillotina.security.security_code import principal_permission_manager as principal_perm_mgr
    from guillotina.security.security_code import principal_role_manager as principal_role_mgr

    nspecified = (
        (principal is not None) +
        (role is not None) +
        (permission is not None) +
        (permissions is not None))
    permspecified = (
        (permission is not None) +
        (permissions is not None))

    if nspecified != 2 or permspecified == 2:
        raise ConfigurationError(
            "Exactly two of the principal, role, and permission resp. "
            "permissions attributes must be specified")

    if permission:
        permissions = [permission]

    if principal and role:
        _context.action(
            discriminator=('grantRoleToPrincipal', role, principal),
            callable=principal_role_mgr.assign_role_to_principal,
            args=(role, principal),
        )
    elif principal and permissions:
        for permission in permissions:
            _context.action(
                discriminator=('grantPermissionToPrincipal',
                               permission,
                               principal),
                callable=principal_perm_mgr.grant_permission_to_principal,
                args=(permission, principal),
            )
    elif role and permissions:
        for permission in permissions:
            _context.action(
                discriminator=('grantPermissionToRole', permission, role),
                callable=role_perm_mgr.grant_permission_to_role,
                args=(permission, role),
            )


def grantAll_directive(_context, principal=None, role=None):
    """Grant all permissions to a role or principal
    """
    from guillotina.security.security_code import role_permission_manager
    from guillotina.security.security_code import principal_permission_manager
    nspecified = (
        (principal is not None) +
        (role is not None))

    if nspecified != 1:
        raise ConfigurationError(
            "Exactly one of the principal and role attributes "
            "must be specified")

    if principal:
        _context.action(
            discriminator=('grantAllPermissionsToPrincipal',
                           principal),
            callable=principal_permission_manager.grantAllPermissionsToPrincipal,
            args=(principal, ),
        )
    else:
        _context.action(
            discriminator=('grantAllPermissionsToRole', role),
            callable=role_permission_manager.grantAllPermissionsToRole,
            args=(role, ),
        )


def defineRole_directive(_context, id, title, description='', local=True):
    from guillotina.auth.role import Role

    role = Role(id, title, description, local)
    component.utility(_context, IRole, role, name=id)


def scan(path):
    """
    pyramid's version of scan has a much more advanced resolver that we
    can look into supporting eventually...
    """
    path = resolve_module_path(path)
    __import__(path)


def clear():
    _registered_configurations[:] = []
