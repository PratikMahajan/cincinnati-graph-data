#!/usr/bin/env python

import argparse
import codecs
import collections
import io
import json
import os
import tarfile

try:
    from urllib.request import Request, urlopen  # Python 3
except ImportError:
    from urllib2 import Request, urlopen  # Python 2


def load_edges(directory, nodes):
    edges = {}
    for root, _, files in os.walk(directory):
        for filename in files:
            if not filename.endswith('.json'):
                continue
            path = os.path.join(root, filename)
            with open(path) as f:
                try:
                    edge = json.load(f)
                except ValueError as e:
                    raise ValueError('failed to load JSON from {}: {}'.format(path, e))
            edge_key = (edge['from'], edge['to'])
            if edge_key in edges:
                raise ValueError('duplicate edges for {} (Quay labels do not support channel granularity)')
            edges[edge_key] = edge
            from_node = nodes[edge['from']]
            to_node = nodes[edge['to']]
            node_channels = set(from_node['channels']).intersection(to_node['channels'])
            if set(edge['channels']) != node_channels:
                raise ValueError('edge channels {} differ from node channels {} (Quay labels do not support channel granularity)'.format(sorted(edge['channels']), sorted(node_channels)))
            if 'previous' in to_node:
                to_node['previous'].add(edge['from'])
            else:
                to_node['previous'] = {edge['from']}
    return nodes


def load_nodes(directory):
    nodes = {}
    for root, _, files in os.walk(directory):
        channel = os.path.basename(root)
        for filename in files:
            if not filename.endswith('.json'):
                continue
            path = os.path.join(root, filename)
            with open(path) as f:
                try:
                    node = json.load(f)
                except ValueError as e:
                    raise ValueError('failed to load JSON from {}: {}'.format(path, e))
            previous_node = nodes.get(node['version'])
            if previous_node:
               previous_node['channels'].add(channel)
               continue
            node['channels'] = {channel}
            nodes[node['version']] = node
    for node in nodes.values():
        if 'metadata' not in node:
            node['metadata'] = {}
        node['metadata']['io.openshift.upgrades.graph.release.channels'] = ','.join(sorted(node['channels']))
    return nodes


def push(directory, token):
    nodes = load_nodes(directory=os.path.join(directory, 'channels'))
    nodes = load_edges(directory=os.path.join(directory, 'edges'), nodes=nodes)
    for node in nodes.values():
        sync_node(node=node, token=token)


def sync_node(node, token):
    labels = get_labels(node=node)

    channels = labels.get('io.openshift.upgrades.graph.release.channels', {}).get('value', '')
    if channels and channels != node['metadata']['io.openshift.upgrades.graph.release.channels']:
        print('label mismatch for {}: {} != {}'.format(node['version'], channels, node['metadata']['io.openshift.upgrades.graph.release.channels']))
        delete_label(
            node=node,
            label='io.openshift.upgrades.graph.release.channels',
            token=token)
        channels = None
    if not channels:
        post_label(
            node=node,
            label={
                'media_type': 'text/plain',
                'key': 'io.openshift.upgrades.graph.release.channels',
                'value': node['metadata']['io.openshift.upgrades.graph.release.channels'],
            },
            token=token)

    for key in ['next.add', 'next.remove']:
        label = 'io.openshift.upgrades.graph.{}'.format(key)
        if label in labels:
            delete_label(node=node, label=label, token=token)

    if node.get('previous', set()):
        meta = get_release_metadata(node=node)
        previous = set(meta.get('previous', set()))
        want_removed = previous - node['previous']
        current_removed = set(version for version in labels.get('io.openshift.upgrades.graph.previous.remove', {}).get('value', '').split(',') if version)
        if current_removed != want_removed:
            print('changing {} previous.remove from {} to {}'.format(node['version'], sorted(current_removed), sorted(want_removed)))
            if 'io.openshift.upgrades.graph.previous.remove' in labels:
                delete_label(node=node, label='io.openshift.upgrades.graph.previous.remove', token=token)
            if want_removed:
                post_label(
                    node=node,
                    label={
                        'media_type': 'text/plain',
                        'key': 'io.openshift.upgrades.graph.previous.remove',
                        'value': ','.join(sorted(want_removed)),
                    },
                    token=token)
        want_added = node['previous'] - previous
        current_added = set(version for version in labels.get('io.openshift.upgrades.graph.previous.add', {}).get('value', '').split(',') if version)
        if current_added != want_added:
            print('changing {} previous.add from {} to {}'.format(node['version'], sorted(current_added), sorted(want_added)))
            if 'io.openshift.upgrades.graph.previous.add' in labels:
                delete_label(node=node, label='io.openshift.upgrades.graph.previous.add', token=token)
            if want_added:
                post_label(
                    node=node,
                    label={
                        'media_type': 'text/plain',
                        'key': 'io.openshift.upgrades.graph.previous.add',
                        'value': ','.join(sorted(want_added)),
                    },
                    token=token)
    else:
        if 'io.openshift.upgrades.graph.previous.add' in labels:
            print('{} had previous additions, but we want no incoming edges'.format(node['version']))
            delete_label(node=node, label='io.openshift.upgrades.graph.previous.add', token=token)
        previous_remove = labels.get('io.openshift.upgrades.graph.previous.remove', {}).get('value', '')
        if previous_remove != '*':
            meta = get_release_metadata(node=node)
            if meta.get('previous', set()):
                print('replacing {} previous remove {!r} with *'.format(node['version'], previous_remove))
                if 'io.openshift.upgrades.graph.previous.remove' in labels:
                    delete_label(node=node, label='io.openshift.upgrades.graph.previous.remove', token=token)
                post_label(
                    node=node,
                    label={
                        'media_type': 'text/plain',
                        'key': 'io.openshift.upgrades.graph.previous.remove',
                        'value': '*',
                    },
                    token=token)


def manifest_uri(node):
    pullspec = node['payload']
    name, digest = pullspec.split('@', 1)
    prefix = 'quay.io/'
    if not name.startswith(prefix):
        raise ValueError('non-Quay pullspec: {}'.format(pullspec))
    name = name[len(prefix):]
    return 'https://quay.io/api/v1/repository/{}/manifest/{}'.format(name, digest)


def get_labels(node):
    f = urlopen('{}/labels'.format(manifest_uri(node=node)))
    data = json.load(codecs.getreader('utf-8')(f))
    f.close()  # no context manager with-statement because in Python 2: AttributeError: addinfourl instance has no attribute '__exit__'
    return {label['key']: label for label in data['labels']}


def delete_label(node, label, token):
    uri = '{}/labels/{}'.format(manifest_uri(node=node), label)
    print('{} {} {}'.format(node['version'], 'delete', uri))
    if not token:
        return  # dry run
    request = Request(uri)
    request.add_header('Authorization', 'Bearer {}'.format(token))
    request.get_method = lambda: 'DELETE'
    return urlopen(request)


def post_label(node, label, token):
    uri = '{}/labels'.format(manifest_uri(node=node))
    print('{} {} {}'.format(node['version'], 'post', uri))
    if not token:
        return  # dry run
    request = Request(uri, json.dumps(label).encode('utf-8'))
    request.add_header('Authorization', 'Bearer {}'.format(token))
    request.add_header('Content-Type', 'application/json')
    return urlopen(request)


def get_release_metadata(node):
    f = urlopen(manifest_uri(node=node))
    data = json.load(codecs.getreader('utf-8')(f))
    f.close()  # no with-statement because in Python 2: AttributeError: addinfourl instance has no attribute '__exit__'
    for layer in reversed(data['layers']):
        digest = layer['blob_digest']
        pullspec = node['payload']
        name = pullspec.split('@', 1)[0]
        prefix = 'quay.io/'
        if not name.startswith(prefix):
            raise ValueError('non-Quay pullspec: {}'.format(pullspec))
        name = name[len(prefix):]
        uri = 'https://quay.io/v2/{}/blobs/{}'.format(name, digest)
        f = urlopen(uri)
        layer_bytes = f.read()
        f.close()

        with tarfile.open(fileobj=io.BytesIO(layer_bytes), mode='r:gz') as tar:
            f = tar.extractfile('release-manifests/release-metadata')
            return json.load(codecs.getreader('utf-8')(f))
    raise ValueError('no release-metadata in {} layers'.format(node['version']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Push graph metadata to Quay.io labels.')
    parser.add_argument(
        '-t', '--token',
        help='Quay token ( https://docs.quay.io/api/#applications-and-tokens )')
    args = parser.parse_args()
    push(directory='.', token=args.token)