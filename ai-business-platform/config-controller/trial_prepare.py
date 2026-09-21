"""Runs in the trusted Agent image; candidate content is data, never Python code."""
from pathlib import Path, PurePosixPath
import hashlib
import json
from app.registry import CapabilityRegistry
from jsonschema import Draft202012Validator


def prepare(bundle, root):
    for doc in bundle['documents']:
        path = PurePosixPath(doc['component']) / doc['path']
        if path.is_absolute() or '..' in path.parts or '\\' in str(path):
            raise ValueError('invalid trial path')
        if hashlib.sha256(doc['content'].encode()).hexdigest() != doc['sha256']:
            raise ValueError('trial document hash mismatch')
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(doc['content'])
    registry = CapabilityRegistry(root/'agent/profiles/capabilities.yaml', root/'agent/profiles', root/'agent/schemas')
    source = bundle['source']
    contexts = []
    for cap in registry.capabilities.values():
        schema = json.loads(registry.loaded_files['schemas/' + cap.schema.name])
        Draft202012Validator.check_schema(schema)
        affected = source['kind'] in {'harness', 'capabilities'}
        affected |= source['component'] == 'agent' and source['path'] in {'profiles/' + cap.profile + '.md', 'schemas/' + cap.schema.name}
        affected |= source['kind'] == 'skill' and source.get('skill_id') in cap.skills
        if affected:
            context = registry.agent_context(cap)
            if context not in contexts:
                contexts.append(context)
    if not contexts or len(contexts) > 16:
        raise ValueError('no bounded runtime coverage for this configuration')
    (root/'contexts.json').write_text(json.dumps(contexts))


if __name__ == '__main__':
    prepare(json.loads(Path('/candidate/bundle.json').read_text()), Path('/work/config'))
