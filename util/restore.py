#!/usr/bin/env python3

import argparse
import inflection
import kubernetes
import logging
import os
import re
import time
import yaml

logging_format = '[%(asctime)s] [%(levelname)s] - %(message)s'
logging_level = os.environ.get('LOGGING_LEVEL', logging.INFO)
logger = None

if os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount'):
    kubernetes.config.load_incluster_config()
else:
    kubernetes.config.load_kube_config()

core_v1_api = kubernetes.client.CoreV1Api()
custom_objects_api = kubernetes.client.CustomObjectsApi()
api_client = core_v1_api.api_client
api_groups = {}

def init_logger():
    global logger
    handler = logging.StreamHandler()
    handler.setLevel(logging_level)
    handler.setFormatter(
        logging.Formatter(logging_format)
    )
    logger = logging.getLogger('restore')
    logger.setLevel(logging_level)
    logger.addHandler(handler)
    logger.propagate = False

def discover_api_group(api_group, version):
    resp = api_client.call_api(
        '/apis/{}/{}'.format(api_group,version), 'GET',
        auth_settings=['BearerToken'], response_type='object'
    )
    group_info = resp[0]
    if api_group not in api_groups:
        api_groups[api_group] = {}
    api_groups[api_group][version] = group_info

def resource_kind_to_plural(api_group, version, kind):
    if not api_group:
        return inflection.pluralize(kind).lower()

    if api_group not in api_groups \
    or version not in api_groups[api_group]:
        discover_api_group(api_group, version)

    for resource in api_groups[api_group][version]['resources']:
        if resource['kind'] == kind:
            return resource['name']
    return None

def restore_file(file_path, restore_status_on):
    logger.info('Restore from %s', file_path)
    with open(file_path) as f:
        documents = yaml.safe_load_all(f)
        for document in documents:
            restore_resource(document, file_path, restore_status_on)

def restore_namespaced_core_v1_resource(namespace, kind, name, resource, file_path):
    underscore_kind = inflection.underscore(kind)
    create_method = getattr(core_v1_api, 'create_namespaced_' + underscore_kind)
    delete_method = getattr(core_v1_api, 'delete_namespaced_' + underscore_kind)
    patch_method = getattr(core_v1_api, 'patch_namespaced_' + underscore_kind)
    read_method = getattr(core_v1_api, 'read_namespaced_' + underscore_kind)

    try:
        current = read_method(name, namespace)
        logger.info('Delete current %s %s in %s', kind, name, namespace)
        if current.metadata.finalizers:
            patch_method(name, namespace, {'metadata': {'finalizers': []}})
        delete_method(name, namespace)
    except kubernetes.client.rest.ApiException as e:
        if e.status != 404:
            raise
    logger.info('Create %s %s in %s', kind, name, namespace)
    create_method(namespace, resource)

def restore_cluster_core_v1_resource(kind, name, resource, file_path):
    underscore_kind = inflection.underscore(kind)
    create_method = getattr(core_v1_api, 'create_' + underscore_kind)
    delete_method = getattr(core_v1_api, 'delete_' + underscore_kind)
    patch_method = getattr(core_v1_api, 'patch_' + underscore_kind)
    read_method = getattr(core_v1_api, 'read_' + underscore_kind)

    try:
        current = read_method(name)
        logger.info('Delete current %s %s', kind, name)
        if current.metadata.finalizers:
            patch_method(name, {'metadata': {'finalizers': []}})
        delete_method(name)
    except kubernetes.client.rest.ApiException as e:
        if e.status != 404:
            raise
    logger.info('Create %s %s', kind, name)
    create_method(resource)

def restore_namespaced_custom_resource(api_group, api_version, namespace, kind, name, resource, file_path, restore_status_on):
    plural = resource_kind_to_plural(api_group, api_version, kind)
    if not plural:
        logger.warning('Unable to determine plural for %s/%s %s', api_group, api_version, kind)
        return

    try:
        current = custom_objects_api.get_namespaced_custom_object(
            api_group, api_version, namespace, plural, name
        )
        logger.info('Delete current %s/%s %s %s', api_group, api_version, kind, name)
        if current['metadata'].get('finalizers'):
            custom_objects_api.patch_namespaced_custom_object(
                api_group, api_version, namespace, plural, name,
                {'metadata': {'finalizers': []}}
            )
        custom_objects_api.delete_namespaced_custom_object(
            api_group, api_version, namespace, plural, name
        )
    except kubernetes.client.rest.ApiException as e:
        if e.status != 404:
            raise

    logger.info('Create %s/%s %s %s', api_group, api_version, kind, name)
    custom_objects_api.create_namespaced_custom_object(
        api_group, api_version, namespace, plural, resource
    )

    if '{}.{}'.format(plural, api_group) in restore_status_on \
    and 'status' in resource:
        custom_objects_api.patch_namespaced_custom_object_status(
            api_group, api_version, namespace, plural, name, resource
        )

def restore_cluster_custom_resource(api_group, api_version, kind, name, resource, file_path, restore_status_on):
    logger.warning('Not implemented')

def restore_resource(resource, file_path, restore_status_on):
    api_version = resource.get('apiVersion')
    if not api_version:
        logger.warning('Resource in %s missing apiVersion, ignoring.')
        return
    kind = resource.get('kind')
    if not kind:
        logger.warning('Resource in %s missing kind, ignoring.')
        return

    if api_version == 'v1' and kind == 'List':
        for item in document.get('items', []):
            restore_resource(item, file_path, restore_status_on)
        return

    metadata = resource.get('metadata', {})
    if not metadata:
        logger.warning('Resource in %s missing metadata, ignoring.')
        return
    name = metadata.get('name', {})
    if not metadata:
        logger.warning('Resource in %s missing metadata.name, ignoring.')
        return
    namespace = metadata.get('namespace', {})
    if api_version == 'v1':
        if namespace:
            restore_namespaced_core_v1_resource(namespace, kind, name, resource, file_path)
        else:
            restore_cluster_core_v1_resource(kind, name, resource, file_path)
    elif '/' in api_version:
        api_group, api_version = api_version.split('/')
        if namespace:
            restore_namespaced_custom_resource(api_group, api_version, namespace, kind, name, resource, file_path, restore_status_on)
        else:
            restore_cluster_custom_resource(api_group, api_version, kind, name, resource, file_path, restore_status_on)
    else:
        logger.warning('Unable to handle apiVersion %s', api_version)

def restore_backup(backup_path, restore_status_on):
    logger.info('Restoring backup from %s', backup_path)
    for root, dirs, files in os.walk(backup_path):
        for filename in files:
            if filename.endswith(".json") \
            or filename.endswith(".yaml") \
            or filename.endswith(".yml"):
                 restore_file(os.path.join(root, filename), restore_status_on)

def stop_operators(operator_list):
    # Check that all operator deployments exist
    for operator in operator_list:
        try:
            deployment = custom_objects_api.get_namespaced_custom_object(
                'apps', 'v1', operator['namespace'], 'deployments', operator['name']
            )
            operator['replicas'] = deployment['spec']['replicas']
            operator['selector'] = deployment['spec']['selector']

        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                logger.error('Unable to find deployment %s in %s', operator['name'], operator['namespace'])
                sys.exit(1)
            else:
                raise

    for operator in operator_list:
        logger.info('Scale deployment %s in %s to 0 replicas', operator['name'], operator['namespace'])
        deployment = custom_objects_api.patch_namespaced_custom_object(
            'apps', 'v1', operator['namespace'], 'deployments', operator['name'],
            {"spec": {"replicas": 0}}
        )

    for operator in operator_list:
        if 'matchLabels' not in operator['selector']:
            logger.warning(
                'Do not know how to find pods for %s in %s, no spec.selector.matchLabels',
                operator['name'], operator['namespace']
            )
            continue

        label_selector = ','.join(["{}={}".format(k, v) for k, v in operator['selector']['matchLabels'].items()])

        attempt = 1
        while True:
            time.sleep(5)
            pods = core_v1_api.list_namespaced_pod(
                operator['namespace'], label_selector=label_selector
            )
            if 0 == len(pods.items):
                break
            attempt += 1
            if attempt > 60:
                raise Exception('Failed to scale down {} in {}'.format(operator['name'], operator['namespace']))
            logger.info('Wait for deployment %s in %s to scale down', operator['name'], operator['namespace'])

def restart_operators(operator_list):
    for operator in operator_list:
        logger.info('Scale deployment %s in %s to %d replicas', operator['name'], operator['namespace'], operator['replicas'])
        deployment = custom_objects_api.patch_namespaced_custom_object(
            'apps', 'v1', operator['namespace'], 'deployments', operator['name'],
            {"spec": {"replicas": operator['replicas']}}
        )

def main():
    import argparse
    backup_path = os.environ.get('BACKUP_PATH', '')

    parser = argparse.ArgumentParser(description='Restore resources from backup.')
    parser.add_argument(
        'backup', metavar='BACKUP_PATH', type=str,
        nargs=(1 if backup_path=='' else '?'), default=backup_path
    )
    parser.add_argument(
        '--restore-status-on', metavar='PLURAL.APIGROUP', type=str, nargs='*', action='append',
        help='List of kinds of resources for which to restore status. Ex: widgets.example.com'
    )
    parser.add_argument(
        '--stop-operators', metavar='NAMESPACE/DEPLOYMENT', type=str, nargs='*', action='append',
        help='List of operators to stop before restart and restart after completion.'
    )
    args = parser.parse_args()

    if args.restore_status_on:
        restore_status_on = [item for sublist in args.restore_status_on for item in sublist]
    elif os.environ.get('RESTORE_STATUS_ON'):
        restore_status_on = re.split(r'[ ,]+', os.environ['RESTORE_STATUS_ON'])
    else:
        restore_status_on = []

    if args.stop_operators:
        stop_operator_list = [item for sublist in args.stop_operators for item in sublist]
    elif os.environ.get('STOP_OPERATORS'):
        stop_operator_list = re.split(r'[ ,]+', os.environ['STOP_OPERATORS'])
    else:
        stop_operator_list = []

    for operator_spec in stop_operator_list:
        if not re.match(r'^[a-z0-9\-]+/[a-z0-9\-]+$', 'foo/bar'):
            print("Invalid value for STOP_OPERATORS, must be in format NAMESPACE/NAME")
            parser.print_help()

    # Convert list to format: [{"namespace": "...", "name": "..."}]
    stop_operator_list = [dict(zip(['namespace', 'name'], item.split('/'))) for item in stop_operator_list]

    init_logger()

    stop_operators(stop_operator_list)
    try:
        restore_backup(args.backup[0], restore_status_on)
    finally:
        restart_operators(stop_operator_list)

if __name__ == '__main__':
    main()
