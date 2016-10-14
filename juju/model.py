import asyncio
import collections
import logging
from concurrent.futures import CancelledError

from .client import client
from .client import watcher
from .client import connection
from .delta import get_entity_delta
from .delta import get_entity_class
from .exceptions import DeadEntityException

log = logging.getLogger(__name__)


class ModelObserver(object):
    async def __call__(self, delta, old, new, model):
        if old is None and new is not None:
            type_ = 'add'
        else:
            type_ = delta.type
        handler_name = 'on_{}_{}'.format(delta.entity, type_)
        method = getattr(self, handler_name, self.on_change)
        await method(delta, old, new, model)

    async def on_change(self, delta, old, new, model):
        pass


class ModelState(object):
    """Holds the state of the model, including the delta history of all
    entities in the model.

    """
    def __init__(self, model):
        self.model = model
        self.state = dict()

    def _live_entity_map(self, entity_type):
        """Return an id:Entity map of all the living entities of
        type ``entity_type``.

        """
        return {
            entity_id: self.get_entity(entity_type, entity_id)
            for entity_id, history in self.state.get(entity_type, {}).items()
            if history[-1] is not None
        }

    @property
    def applications(self):
        """Return a map of application-name:Application for all applications
        currently in the model.

        """
        return self._live_entity_map('application')

    @property
    def machines(self):
        """Return a map of machine-id:Machine for all machines currently in
        the model.

        """
        return self._live_entity_map('machine')

    @property
    def units(self):
        """Return a map of unit-id:Unit for all units currently in
        the model.

        """
        return self._live_entity_map('unit')

    def entity_history(self, entity_type, entity_id):
        """Return the history deque for an entity.

        """
        return self.state[entity_type][entity_id]

    def entity_data(self, entity_type, entity_id, history_index):
        """Return the data dict for an entity at a specific index of its
        history.

        """
        return self.entity_history(entity_type, entity_id)[history_index]

    def apply_delta(self, delta):
        """Apply delta to our state and return a copy of the
        affected object as it was before and after the update, e.g.:

            old_obj, new_obj = self.apply_delta(delta)

        old_obj may be None if the delta is for the creation of a new object,
        e.g. a new application or unit is deployed.

        new_obj will never be None, but may be dead (new_obj.dead == True)
        if the object was deleted as a result of the delta being applied.

        """
        history = (
            self.state
            .setdefault(delta.entity, {})
            .setdefault(delta.get_id(), collections.deque())
        )

        history.append(delta.data)
        if delta.type == 'remove':
            history.append(None)

        entity = self.get_entity(delta.entity, delta.get_id())
        return entity.previous(), entity

    def get_entity(
            self, entity_type, entity_id, history_index=-1, connected=True):
        """Return an object instance representing the entity created or
        updated by ``delta``

        """
        if history_index < 0 and history_index != -1:
            history_index += len(self.entity_history(entity_type, entity_id))

        try:
            self.entity_data(entity_type, entity_id, history_index)
        except IndexError:
            return None

        entity_class = get_entity_class(entity_type)
        return entity_class(
            entity_id, self.model, history_index=history_index,
            connected=connected)


class ModelEntity(object):
    """An object in the Model tree"""

    def __init__(self, entity_id, model, history_index=-1, connected=True):
        """Initialize a new entity

        :param entity_id str: The unique id of the object in the model
        :param model: The model instance in whose object tree this
            entity resides
        :history_index int: The index of this object's state in the model's
            history deque for this entity
        :connected bool: Flag indicating whether this object gets live updates
            from the model.

        """
        self.entity_id = entity_id
        self.model = model
        self._history_index = history_index
        self.connected = connected
        self.connection = model.connection

    def __getattr__(self, name):
        """Fetch object attributes from the underlying data dict held in the
        model.

        """
        if self.data is None:
            raise DeadEntityException(
                "Entity {}:{} is dead - its attributes can no longer be "
                "accessed. Use the .previous() method on this object to get "
                "a copy of the object at its previous state.".format(
                    self.entity_type, self.entity_id))
        return self.data[name]

    @property
    def entity_type(self):
        """A string identifying the entity type of this object, e.g.
        'application' or 'unit', etc.

        """
        return self.__class__.__name__.lower()

    @property
    def current(self):
        """Return True if this object represents the current state of the
        entity in the underlying model.

        This will be True except when the object represents an entity at a
        prior state in history, e.g. if the object was obtained by calling
        .previous() on another object.

        """
        return self._history_index == -1

    @property
    def dead(self):
        """Returns True if this entity no longer exists in the underlying
        model.

        """
        return (
            self.data is None or
            self.model.state.entity_data(
                self.entity_type, self.entity_id, -1) is None
        )

    @property
    def alive(self):
        """Returns True if this entity still exists in the underlying
        model.

        """
        return not self.dead

    @property
    def data(self):
        """The data dictionary for this entity.

        """
        return self.model.state.entity_data(
            self.entity_type, self.entity_id, self._history_index)

    def previous(self):
        """Return a copy of this object as was at its previous state in
        history.

        Returns None if this object is new (and therefore has no history).

        The returned object is always "disconnected", i.e. does not receive
        live updates.

        """
        return self.model.state.get_entity(
            self.entity_type, self.entity_id, self._history_index - 1,
            connected=False)

    def next(self):
        """Return a copy of this object at its next state in
        history.

        Returns None if this object is already the latest.

        The returned object is "disconnected", i.e. does not receive
        live updates, unless it is current (latest).

        """
        if self._history_index == -1:
            return None

        new_index = self._history_index + 1
        connected = (
            new_index == len(self.model.state.entity_history(
                self.entity_type, self.entity_id)) - 1
        )
        return self.model.state.get_entity(
            self.entity_type, self.entity_id, self._history_index - 1,
            connected=connected)

    def latest(self):
        """Return a copy of this object at its current state in the model.

        Returns self if this object is already the latest.

        The returned object is always "connected", i.e. receives
        live updates from the model.

        """
        if self._history_index == -1:
            return self

        return self.model.state.get_entity(self.entity_type, self.entity_id)


class Model(object):
    def __init__(self, loop=None):
        """Instantiate a new connected Model.

        :param loop: an asyncio event loop

        """
        self.loop = loop or asyncio.get_event_loop()
        self.connection = None
        self.observers = set()
        self.state = ModelState(self)
        self._watcher_task = None
        self._watch_shutdown = asyncio.Event(loop=loop)
        self._watch_received = asyncio.Event(loop=loop)

    async def connect_current(self):
        """Connect to the current Juju model.

        """
        self.connection = await connection.Connection.connect_current()
        self._watch()
        await self._watch_received.wait()

    async def disconnect(self):
        """Shut down the watcher task and close websockets.

        """
        self._stop_watching()
        if self.connection and self.connection.is_open:
            await self._watch_shutdown.wait()
            log.debug('Closing model connection')
            await self.connection.close()
            self.connection = None

    def all_units_idle(self):
        """Return True if all units are idle.

        """
        for unit in self.units.values():
            unit_status = unit.data['agent-status']['current']
            if unit_status != 'idle':
                return False
        return True

    async def reset(self, force=False):
        """Reset the model to a clean state.

        :param bool force: Force-terminate machines.

        This returns only after the model has reached a clean state. "Clean"
        means no applications or machines exist in the model.

        """
        log.debug('Resetting model')
        for app in self.applications.values():
            await app.destroy()
        for machine in self.machines.values():
            await machine.destroy(force=force)
        await self.block_until(
            lambda: len(self.machines) == 0
        )

    async def block_until(self, *conditions, timeout=None):
        """Return only after all conditions are true.

        """
        async def _block():
            while not all(c() for c in conditions):
                await asyncio.sleep(.1)
        await asyncio.wait_for(_block(), timeout)

    @property
    def applications(self):
        """Return a map of application-name:Application for all applications
        currently in the model.

        """
        return self.state.applications

    @property
    def machines(self):
        """Return a map of machine-id:Machine for all machines currently in
        the model.

        """
        return self.state.machines

    @property
    def units(self):
        """Return a map of unit-id:Unit for all units currently in
        the model.

        """
        return self.state.units

    def add_observer(self, callable_):
        """Register an "on-model-change" callback

        Once a watch is started (Model.watch() is called), ``callable_``
        will be called each time the model changes. callable_ should
        be Awaitable and accept the following positional arguments:

            delta - An instance of :class:`juju.delta.EntityDelta`
                containing the raw delta data recv'd from the Juju
                websocket.

            old_obj - If the delta modifies an existing object in the model,
                old_obj will be a copy of that object, as it was before the
                delta was applied. Will be None if the delta creates a new
                entity in the model.

            new_obj - A copy of the new or updated object, after the delta
                is applied. Will be None if the delta removes an entity
                from the model.

            model - The :class:`Model` itself.

        """
        self.observers.add(callable_)

    def _watch(self):
        """Start an asynchronous watch against this model.

        See :meth:`add_observer` to register an onchange callback.

        """
        async def _start_watch():
            self._watch_shutdown.clear()
            try:
                allwatcher = watcher.AllWatcher()
                self._watch_conn = await self.connection.clone()
                allwatcher.connect(self._watch_conn)
                while True:
                    results = await allwatcher.Next()
                    for delta in results.deltas:
                        delta = get_entity_delta(delta)
                        old_obj, new_obj = self.state.apply_delta(delta)
                        # XXX: Might not want to shield at this level
                        # We are shielding because when the watcher is
                        # canceled (on disconnect()), we don't want all of
                        # its children (every observer callback) to be
                        # canceled with it. So we shield them. But this means
                        # they can *never* be canceled.
                        await asyncio.shield(
                            self._notify_observers(delta, old_obj, new_obj))
                    self._watch_received.set()
            except CancelledError:
                log.debug('Closing watcher connection')
                await self._watch_conn.close()
                self._watch_shutdown.set()
                self._watch_conn = None

        log.debug('Starting watcher task')
        self._watcher_task = self.loop.create_task(_start_watch())

    def _stop_watching(self):
        """Stop the asynchronous watch against this model.

        """
        log.debug('Stopping watcher task')
        if self._watcher_task:
            self._watcher_task.cancel()

    async def _notify_observers(self, delta, old_obj, new_obj):
        """Call observing callbacks, notifying them of a change in model state

        :param delta: The raw change from the watcher
            (:class:`juju.client.overrides.Delta`)
        :param old_obj: The object in the model that this delta updates.
            May be None.
        :param new_obj: The object in the model that is created or updated
            by applying this delta.

        """
        log.debug(
            'Model changed: %s %s %s',
            delta.entity, delta.type, delta.get_id())
        for o in self.observers:
            asyncio.ensure_future(o(delta, old_obj, new_obj, self))

    def add_machine(
            self, spec=None, constraints=None, disks=None, series=None,
            count=1):
        """Start a new, empty machine and optionally a container, or add a
        container to a machine.

        :param str spec: Machine specification
            Examples::

                (None) - starts a new machine
                'lxc' - starts a new machine with on lxc container
                'lxc:4' - starts a new lxc container on machine 4
                'ssh:user@10.10.0.3' - manually provisions a machine with ssh
                'zone=us-east-1a' - starts a machine in zone us-east-1s on AWS
                'maas2.name' - acquire machine maas2.name on MAAS
        :param constraints: Machine constraints
        :type constraints: :class:`juju.Constraints`
        :param list disks: List of disk :class:`constraints <juju.Constraints>`
        :param str series: Series
        :param int count: Number of machines to deploy

        Supported container types are: lxc, lxd, kvm

        When deploying a container to an existing machine, constraints cannot
        be used.

        """
        pass
    add_machines = add_machine

    async def add_relation(self, relation1, relation2):
        """Add a relation between two applications.

        :param str relation1: '<application>[:<relation_name>]'
        :param str relation2: '<application>[:<relation_name>]'

        """
        app_facade = client.ApplicationFacade()
        app_facade.connect(self.connection)

        log.debug(
            'Adding relation %s <-> %s', relation1, relation2)

        return await app_facade.AddRelation([relation1, relation2])

    def add_space(self, name, *cidrs):
        """Add a new network space.

        Adds a new space with the given name and associates the given
        (optional) list of existing subnet CIDRs with it.

        :param str name: Name of the space
        :param \*cidrs: Optional list of existing subnet CIDRs

        """
        pass

    def add_ssh_key(self, key):
        """Add a public SSH key to this model.

        :param str key: The public ssh key

        """
        pass
    add_ssh_keys = add_ssh_key

    def add_subnet(self, cidr_or_id, space, *zones):
        """Add an existing subnet to this model.

        :param str cidr_or_id: CIDR or provider ID of the existing subnet
        :param str space: Network space with which to associate
        :param str \*zones: Zone(s) in which the subnet resides

        """
        pass

    def get_backups(self):
        """Retrieve metadata for backups in this model.

        """
        pass

    def block(self, *commands):
        """Add a new block to this model.

        :param str \*commands: The commands to block. Valid values are
            'all-changes', 'destroy-model', 'remove-object'

        """
        pass

    def get_blocks(self):
        """List blocks for this model.

        """
        pass

    def get_cached_images(self, arch=None, kind=None, series=None):
        """Return a list of cached OS images.

        :param str arch: Filter by image architecture
        :param str kind: Filter by image kind, e.g. 'lxd'
        :param str series: Filter by image series, e.g. 'xenial'

        """
        pass

    def create_backup(self, note=None, no_download=False):
        """Create a backup of this model.

        :param str note: A note to store with the backup
        :param bool no_download: Do not download the backup archive
        :return str: Path to downloaded archive

        """
        pass

    def create_storage_pool(self, name, provider_type, **pool_config):
        """Create or define a storage pool.

        :param str name: Name to give the storage pool
        :param str provider_type: Pool provider type
        :param \*\*pool_config: key/value pool configuration pairs

        """
        pass

    def debug_log(
            self, no_tail=False, exclude_module=None, include_module=None,
            include=None, level=None, limit=0, lines=10, replay=False,
            exclude=None):
        """Get log messages for this model.

        :param bool no_tail: Stop after returning existing log messages
        :param list exclude_module: Do not show log messages for these logging
            modules
        :param list include_module: Only show log messages for these logging
            modules
        :param list include: Only show log messages for these entities
        :param str level: Log level to show, valid options are 'TRACE',
            'DEBUG', 'INFO', 'WARNING', 'ERROR,
        :param int limit: Return this many of the most recent (possibly
            filtered) lines are shown
        :param int lines: Yield this many of the most recent lines, and keep
            yielding
        :param bool replay: Yield the entire log, and keep yielding
        :param list exclude: Do not show log messages for these entities

        """
        pass

    async def deploy(
            self, entity_url, service_name=None, bind=None, budget=None,
            channel=None, config=None, constraints=None, force=False,
            num_units=1, plan=None, resources=None, series=None, storage=None,
            to=None):
        """Deploy a new service or bundle.

        :param str entity_url: Charm or bundle url
        :param str service_name: Name to give the service
        :param dict bind: <charm endpoint>:<network space> pairs
        :param dict budget: <budget name>:<limit> pairs
        :param str channel: Charm store channel from which to retrieve
            the charm or bundle, e.g. 'development'
        :param dict config: Charm configuration dictionary
        :param constraints: Service constraints
        :type constraints: :class:`juju.Constraints`
        :param bool force: Allow charm to be deployed to a machine running
            an unsupported series
        :param int num_units: Number of units to deploy
        :param str plan: Plan under which to deploy charm
        :param dict resources: <resource name>:<file path> pairs
        :param str series: Series on which to deploy
        :param dict storage: Storage constraints TODO how do these look?
        :param str to: Placement directive, e.g.::

            '23' - machine 23
            'lxc:7' - new lxc container on machine 7
            '24/lxc/3' - lxc container 3 or machine 24

            If None, a new machine is provisioned.


        TODO::

            - entity_url must have a revision; look up latest automatically if
              not provided by caller
            - service_name is required; fill this in automatically if not
              provided by caller
            - series is required; how do we pick a default?

        """
        if constraints:
            constraints = client.Value(**constraints)

        if to:
            placement = [
                client.Placement(**p) for p in to
            ]
        else:
            placement = []

        if storage:
            storage = {
                k: client.Constraints(**v)
                for k, v in storage.items()
            }

        app_facade = client.ApplicationFacade()
        client_facade = client.ClientFacade()
        app_facade.connect(self.connection)
        client_facade.connect(self.connection)

        log.debug(
            'Deploying %s', entity_url)

        await client_facade.AddCharm(channel, entity_url)
        app = client.ApplicationDeploy(
            application=service_name,
            channel=channel,
            charm_url=entity_url,
            config=config,
            constraints=constraints,
            endpoint_bindings=bind,
            num_units=num_units,
            placement=placement,
            resources=resources,
            series=series,
            storage=storage,
        )

        return await app_facade.Deploy([app])

    def destroy(self):
        """Terminate all machines and resources for this model.

        """
        pass

    def get_backup(self, archive_id):
        """Download a backup archive file.

        :param str archive_id: The id of the archive to download
        :return str: Path to the archive file

        """
        pass

    def enable_ha(
            self, num_controllers=0, constraints=None, series=None, to=None):
        """Ensure sufficient controllers exist to provide redundancy.

        :param int num_controllers: Number of controllers to make available
        :param constraints: Constraints to apply to the controller machines
        :type constraints: :class:`juju.Constraints`
        :param str series: Series of the controller machines
        :param list to: Placement directives for controller machines, e.g.::

            '23' - machine 23
            'lxc:7' - new lxc container on machine 7
            '24/lxc/3' - lxc container 3 or machine 24

            If None, a new machine is provisioned.

        """
        pass

    def get_config(self):
        """Return the configuration settings for this model.

        """
        pass

    def get_constraints(self):
        """Return the machine constraints for this model.

        """
        pass

    def grant(self, username, acl='read'):
        """Grant a user access to this model.

        :param str username: Username
        :param str acl: Access control ('read' or 'write')

        """
        pass

    def import_ssh_key(self, identity):
        """Add a public SSH key from a trusted indentity source to this model.

        :param str identity: User identity in the form <lp|gh>:<username>

        """
        pass
    import_ssh_keys = import_ssh_key

    def get_machines(self, machine, utc=False):
        """Return list of machines in this model.

        :param str machine: Machine id, e.g. '0'
        :param bool utc: Display time as UTC in RFC3339 format

        """
        pass

    def get_shares(self):
        """Return list of all users with access to this model.

        """
        pass

    def get_spaces(self):
        """Return list of all known spaces, including associated subnets.

        """
        pass

    def get_ssh_key(self):
        """Return known SSH keys for this model.

        """
        pass
    get_ssh_keys = get_ssh_key

    def get_storage(self, filesystem=False, volume=False):
        """Return details of storage instances.

        :param bool filesystem: Include filesystem storage
        :param bool volume: Include volume storage

        """
        pass

    def get_storage_pools(self, names=None, providers=None):
        """Return list of storage pools.

        :param list names: Only include pools with these names
        :param list providers: Only include pools for these providers

        """
        pass

    def get_subnets(self, space=None, zone=None):
        """Return list of known subnets.

        :param str space: Only include subnets in this space
        :param str zone: Only include subnets in this zone

        """
        pass

    def remove_blocks(self):
        """Remove all blocks from this model.

        """
        pass

    def remove_backup(self, backup_id):
        """Delete a backup.

        :param str backup_id: The id of the backup to remove

        """
        pass

    def remove_cached_images(self, arch=None, kind=None, series=None):
        """Remove cached OS images.

        :param str arch: Architecture of the images to remove
        :param str kind: Image kind to remove, e.g. 'lxd'
        :param str series: Image series to remove, e.g. 'xenial'

        """
        pass

    def remove_machine(self, *machine_ids):
        """Remove a machine from this model.

        :param str \*machine_ids: Ids of the machines to remove

        """
        pass
    remove_machines = remove_machine

    def remove_ssh_key(self, *keys):
        """Remove a public SSH key(s) from this model.

        :param str \*keys: Keys to remove

        """
        pass
    remove_ssh_keys = remove_ssh_key

    def restore_backup(
            self, bootstrap=False, constraints=None, archive=None,
            backup_id=None, upload_tools=False):
        """Restore a backup archive to a new controller.

        :param bool bootstrap: Bootstrap a new state machine
        :param constraints: Model constraints
        :type constraints: :class:`juju.Constraints`
        :param str archive: Path to backup archive to restore
        :param str backup_id: Id of backup to restore
        :param bool upload_tools: Upload tools if bootstrapping a new machine

        """
        pass

    def retry_provisioning(self):
        """Retry provisioning for failed machines.

        """
        pass

    def revoke(self, username, acl='read'):
        """Revoke a user's access to this model.

        :param str username: Username to revoke
        :param str acl: Access control ('read' or 'write')

        """
        pass

    def run(self, command, timeout=None):
        """Run command on all machines in this model.

        :param str command: The command to run
        :param int timeout: Time to wait before command is considered failed

        """
        pass

    def set_config(self, **config):
        """Set configuration keys on this model.

        :param \*\*config: Config key/values

        """
        pass

    def set_constraints(self, constraints):
        """Set machine constraints on this model.

        :param :class:`juju.Constraints` constraints: Machine constraints

        """
        pass

    def get_action_output(self, action_uuid, wait=-1):
        """Get the results of an action by ID.

        :param str action_uuid: Id of the action
        :param int wait: Time in seconds to wait for action to complete

        """
        pass

    def get_action_status(self, uuid_or_prefix=None, name=None):
        """Get the status of all actions, filtered by ID, ID prefix, or action name.

        :param str uuid_or_prefix: Filter by action uuid or prefix
        :param str name: Filter by action name

        """
        pass

    def get_budget(self, budget_name):
        """Get budget usage info.

        :param str budget_name: Name of budget

        """
        pass

    def get_status(self, filter_=None, utc=False):
        """Return the status of the model.

        :param str filter_: Service or unit name or wildcard ('*')
        :param bool utc: Display time as UTC in RFC3339 format

        """
        pass
    status = get_status

    def sync_tools(
            self, all_=False, destination=None, dry_run=False, public=False,
            source=None, stream=None, version=None):
        """Copy Juju tools into this model.

        :param bool all_: Copy all versions, not just the latest
        :param str destination: Path to local destination directory
        :param bool dry_run: Don't do the actual copy
        :param bool public: Tools are for a public cloud, so generate mirrors
            information
        :param str source: Path to local source directory
        :param str stream: Simplestreams stream for which to sync metadata
        :param str version: Copy a specific major.minor version

        """
        pass

    def unblock(self, *commands):
        """Unblock an operation that would alter this model.

        :param str \*commands: The commands to unblock. Valid values are
            'all-changes', 'destroy-model', 'remove-object'

        """
        pass

    def unset_config(self, *keys):
        """Unset configuration on this model.

        :param str \*keys: The keys to unset

        """
        pass

    def upgrade_gui(self):
        """Upgrade the Juju GUI for this model.

        """
        pass

    def upgrade_juju(
            self, dry_run=False, reset_previous_upgrade=False,
            upload_tools=False, version=None):
        """Upgrade Juju on all machines in a model.

        :param bool dry_run: Don't do the actual upgrade
        :param bool reset_previous_upgrade: Clear the previous (incomplete)
            upgrade status
        :param bool upload_tools: Upload local version of tools
        :param str version: Upgrade to a specific version

        """
        pass

    def upload_backup(self, archive_path):
        """Store a backup archive remotely in Juju.

        :param str archive_path: Path to local archive

        """
        pass
