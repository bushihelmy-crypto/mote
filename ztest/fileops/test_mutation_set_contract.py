from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

import pytest

from mote.contracts.fileops.models import (
    AbsentVersion,
    BlobRef,
    CreateMutation,
    DeleteMutation,
    FileSnapshot,
    MutationSet,
    NameIdentity,
    PathToken,
    PresentVersion,
    ProjectIdentity,
    RecoveryPolicy,
    ReplaceMutation,
    TargetIdentity,
    validate_committed_versions,
)


def _name(key: str) -> NameIdentity:
    return NameIdentity(key=key, scheme="name-v1")


def _target(key: str) -> TargetIdentity:
    return TargetIdentity(key=key, scheme="target-v1")


def _project(key: str = "project") -> ProjectIdentity:
    return ProjectIdentity(key=key, scheme="project-v1")


def _path(name: str) -> PathToken:
    return PathToken(display=f"/{name}", native=f"/{name}")


def _blob(digest: str) -> BlobRef:
    return BlobRef(digest=sha256(digest.encode("utf-8")).hexdigest(), size=1)


def _present(
    name: NameIdentity,
    target: TargetIdentity,
    *,
    digest: str = "before",
) -> PresentVersion:
    return PresentVersion(
        name_identity=name,
        target_identity=target,
        size=1,
        mtime_ns=1,
        digest=digest,
        metadata_digest="metadata",
    )


def _snapshot(
    name: NameIdentity,
    target: TargetIdentity,
    *,
    project: ProjectIdentity | None = None,
) -> FileSnapshot:
    path = _path(name.key)
    artifact = _blob(f"artifact-{name.key}")
    metadata = _blob(f"metadata-{name.key}")
    return FileSnapshot(
        requested_path=path,
        target_path=path,
        project_identity=project or _project(),
        version=PresentVersion(
            name_identity=name,
            target_identity=target,
            size=artifact.size,
            mtime_ns=1,
            digest=artifact.digest,
            metadata_digest=metadata.digest,
        ),
        artifact=artifact,
        metadata=metadata,
    )


def _create(
    name: NameIdentity,
    *,
    project: ProjectIdentity | None = None,
) -> CreateMutation:
    path = _path(name.key)
    return CreateMutation(
        requested_path=path,
        target_path=path,
        project_identity=project or _project(),
        expected_version=AbsentVersion(name_identity=name),
        after=_blob(f"after-{name.key}"),
        metadata=_blob(f"metadata-{name.key}"),
    )


def _replace(
    name: NameIdentity,
    target: TargetIdentity,
    *,
    project: ProjectIdentity | None = None,
) -> ReplaceMutation:
    return ReplaceMutation(
        before=_snapshot(name, target, project=project),
        after=_blob(f"after-{name.key}"),
    )


def _delete(
    name: NameIdentity,
    target: TargetIdentity,
    *,
    project: ProjectIdentity | None = None,
) -> DeleteMutation:
    return DeleteMutation(before=_snapshot(name, target, project=project))


def _mutation_set(*mutations, **overrides) -> MutationSet:
    values = {
        "transaction_id": "transaction",
        "session_id": "session",
        "source": "Edit",
        "mutations": tuple(mutations),
        "recovery_policy": RecoveryPolicy.ROLLBACK_INCOMPLETE,
    }
    values.update(overrides)
    return MutationSet(**values)


def test_recovery_policy_is_a_closed_rollback_incomplete_contract():
    assert tuple(RecoveryPolicy) == (RecoveryPolicy.ROLLBACK_INCOMPLETE,)
    assert RecoveryPolicy.ROLLBACK_INCOMPLETE.value == "rollback_incomplete"

    with pytest.raises((TypeError, ValueError)):
        _mutation_set(
            _create(_name("file")),
            recovery_policy="rollback_incomplete",
        )


def test_mutation_set_requires_a_nonempty_tuple():
    with pytest.raises(ValueError):
        _mutation_set()

    with pytest.raises((TypeError, ValueError)):
        _mutation_set(
            mutations=[_create(_name("file"))],
        )


def test_mutation_set_canonically_sorts_multi_project_scope():
    project_a = _project("a-project")
    project_z = _project("z-project")
    z_file = _create(_name("z-file"), project=project_a)
    other_project = _create(_name("other-file"), project=project_z)
    a_file = _create(_name("a-file"), project=project_a)

    mutation_set = _mutation_set(z_file, other_project, a_file)

    assert mutation_set.mutations == (a_file, z_file, other_project)


def test_mutation_set_rejects_duplicate_name_identity():
    shared_name = _name("shared")

    with pytest.raises(ValueError, match="name"):
        _mutation_set(
            _create(shared_name),
            _replace(shared_name, _target("existing")),
        )


def test_mutation_set_rejects_duplicate_target_identity():
    shared_target = _target("shared")

    with pytest.raises(ValueError, match="target"):
        _mutation_set(
            _replace(_name("first"), shared_target),
            _delete(_name("second"), shared_target),
        )


@pytest.mark.parametrize("field", ("transaction_id", "session_id", "source"))
@pytest.mark.parametrize("invalid", ("", None, 7, True, b"value"))
def test_mutation_set_requires_strict_nonempty_text_fields(field, invalid):
    with pytest.raises((TypeError, ValueError)):
        _mutation_set(_create(_name("file")), **{field: invalid})


@pytest.mark.parametrize("field", ("key", "scheme"))
def test_mutation_set_rejects_invalid_project_identity_scope(field):
    project = replace(_project(), **{field: ""})

    with pytest.raises(ValueError):
        _mutation_set(_create(_name("file"), project=project))


@pytest.mark.parametrize("field", ("key", "scheme"))
def test_mutation_set_rejects_invalid_name_identity_scope(field):
    name = replace(_name("file"), **{field: ""})

    with pytest.raises(ValueError):
        _mutation_set(_create(name))


@pytest.mark.parametrize("field", ("key", "scheme"))
def test_mutation_set_rejects_invalid_target_identity_scope(field):
    target = replace(_target("file"), **{field: ""})

    with pytest.raises(ValueError):
        _mutation_set(_replace(_name("file"), target))


def test_committed_versions_validate_positionally_against_mutations():
    create_name = _name("create")
    replace_name = _name("replace")
    delete_name = _name("delete")
    mutation_set = _mutation_set(
        _create(create_name),
        _replace(replace_name, _target("replace-before")),
        _delete(delete_name, _target("delete-before")),
    )
    versions_by_name = {
        create_name: _present(
            create_name,
            _target("create-after"),
            digest="create-after",
        ),
        replace_name: _present(
            replace_name,
            _target("replace-after"),
            digest="replace-after",
        ),
        delete_name: AbsentVersion(name_identity=delete_name),
    }
    versions = tuple(versions_by_name[mutation.expected_version.name_identity] for mutation in mutation_set.mutations)

    validate_committed_versions(mutation_set, versions)


@pytest.mark.parametrize("length_delta", (-1, 1))
def test_committed_versions_require_exactly_one_version_per_mutation(length_delta):
    first_name = _name("first")
    second_name = _name("second")
    mutation_set = _mutation_set(_create(first_name), _create(second_name))
    versions = (
        _present(first_name, _target("first-after")),
        _present(second_name, _target("second-after")),
    )
    candidate = versions[: 2 + length_delta]
    if length_delta > 0:
        candidate += (_present(_name("extra"), _target("extra-after")),)

    with pytest.raises(ValueError):
        validate_committed_versions(mutation_set, candidate)


def test_committed_versions_reject_noncanonical_container():
    name = _name("file")
    mutation_set = _mutation_set(_create(name))

    with pytest.raises((TypeError, ValueError)):
        validate_committed_versions(
            mutation_set,
            [_present(name, _target("after"))],
        )


@pytest.mark.parametrize(
    ("mutation", "committed"),
    [
        (
            _create(_name("create")),
            AbsentVersion(name_identity=_name("create")),
        ),
        (
            _replace(_name("replace"), _target("replace-before")),
            AbsentVersion(name_identity=_name("replace")),
        ),
        (
            _delete(_name("delete"), _target("delete-before")),
            _present(_name("delete"), _target("delete-after")),
        ),
    ],
)
def test_committed_version_presence_must_match_mutation_kind(mutation, committed):
    with pytest.raises(ValueError):
        validate_committed_versions(_mutation_set(mutation), (committed,))


def test_committed_versions_require_same_name_identity_at_each_position():
    first = _create(_name("first"))
    second = _create(_name("second"))
    mutation_set = _mutation_set(first, second)
    swapped = (
        _present(_name("second"), _target("second-after")),
        _present(_name("first"), _target("first-after")),
    )

    with pytest.raises(ValueError):
        validate_committed_versions(mutation_set, swapped)
