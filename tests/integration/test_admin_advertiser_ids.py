"""The advertiser form's ID field persists within the addressed tenant.

The contract is templates/create_principal.html. These are real Flask and
PostgreSQL tests because the protocol BDD harness does not drive admin forms.
"""

import re
from unittest.mock import patch

import pytest
from sqlalchemy import select

from src.admin.blueprints import principals as principals_blueprint
from src.core.database.models import AuditLog, Principal
from src.core.database.repositories.principal import PrincipalRepository
from tests.factories import PrincipalFactory, TenantFactory
from tests.harness._base import BareIntegrationEnv
from tests.helpers import concurrent_commit_in_write_window, operator_answer
from tests.helpers.admin_session import admin_auth_session

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


@pytest.fixture
def creation_env(integration_db, admin_client):
    with BareIntegrationEnv() as env:
        tenant, _ = env.setup_default_data()
        admin_auth_session(admin_client, tenant.tenant_id)
        yield env, admin_client, tenant


def create(client, tenant_id, *, script_name="", **fields):
    return client.post(
        f"/tenant/{tenant_id}/principals/create",
        data={"name": "Created Advertiser", "enable_mock": "1", **fields},
        environ_overrides={"SCRIPT_NAME": script_name},
    )


def created_rows(env, tenant_id):
    env._session.expire_all()
    return env._session.scalars(select(Principal).filter_by(tenant_id=tenant_id, name="Created Advertiser")).all()


def audited_outcomes(env, tenant_id):
    session = env.get_session()
    session.expire_all()
    return session.scalars(
        select(AuditLog.success).filter_by(tenant_id=tenant_id, operation="AdminUI.create_principal")
    ).all()


@pytest.mark.parametrize("principal_id", ["Buyer_123-brand", "a" * 50, "Anonymous"])
def test_supplied_id_is_preserved(creation_env, principal_id):
    env, client, tenant = creation_env
    response = create(client, tenant.tenant_id, principal_id=principal_id)
    assert response.status_code == 302
    assert response.headers["Location"] == f"/tenant/{tenant.tenant_id}/settings/advertisers"
    (row,) = created_rows(env, tenant.tenant_id)
    assert row.principal_id == principal_id
    assert row.platform_mappings["mock"]["advertiser_id"] == f"mock_{principal_id}"


@pytest.mark.parametrize("principal_id", ["", "   ", "two words", "a/b", "☃", "a" * 51])
@pytest.mark.parametrize("script_name", ["", "/console"])
def test_invalid_id_cannot_create_a_principal(creation_env, principal_id, script_name):
    env, client, tenant = creation_env
    response = create(client, tenant.tenant_id, principal_id=principal_id, script_name=script_name)
    assert operator_answer(client, response) == (
        302,
        f"{script_name}/tenant/{tenant.tenant_id}/principals/create",
        [("error", "Advertiser ID must contain 1 to 50 letters, numbers, underscores, or hyphens")],
    )
    assert created_rows(env, tenant.tenant_id) == []


def test_create_form_enforces_the_server_rule(creation_env):
    _, client, tenant = creation_env
    page = client.get(f"/tenant/{tenant.tenant_id}/principals/create").get_data(as_text=True)
    (field,) = re.findall(r'<input[^>]*\bid="principal_id"[^>]*>', page)
    assert 'maxlength="50"' in field
    assert r'pattern="[a-zA-Z0-9_\-]{1,50}"' in field


@pytest.mark.parametrize("script_name", ["", "/console"])
def test_existing_id_does_not_change_or_duplicate_a_principal(creation_env, script_name):
    env, client, tenant = creation_env
    create(client, tenant.tenant_id, principal_id="buyer")
    (original,) = created_rows(env, tenant.tenant_id)
    original_hash = original.token_hash
    response = create(client, tenant.tenant_id, principal_id="buyer", name="Replacement", script_name=script_name)
    assert response.status_code == 302
    assert response.headers["Location"] == f"{script_name}/tenant/{tenant.tenant_id}/principals/create"
    (row,) = created_rows(env, tenant.tenant_id)
    assert (row.principal_id, row.token_hash) == ("buyer", original_hash)
    assert env._session.scalar(select(Principal).filter_by(tenant_id=tenant.tenant_id, name="Replacement")) is None


def test_race_loser_gets_the_precheck_answer(creation_env):
    _, client, tenant = creation_env
    with concurrent_commit_in_write_window(
        principals_blueprint, lambda: PrincipalFactory(tenant=tenant, principal_id="buyer")
    ):
        loser = operator_answer(client, create(client, tenant.tenant_id, principal_id="buyer"))
    winner = operator_answer(client, create(client, tenant.tenant_id, principal_id="buyer"))
    assert winner[2] == [("error", "Advertiser ID is already in use")]
    assert loser == winner


@pytest.mark.parametrize("principal_id", ["anonymous", "unknown", "admin"])
def test_audit_placeholder_ids_are_reserved(creation_env, principal_id):
    env, client, tenant = creation_env
    answer = operator_answer(client, create(client, tenant.tenant_id, principal_id=principal_id))
    assert answer[2] == [("error", f"Advertiser ID '{principal_id}' is reserved")]
    assert created_rows(env, tenant.tenant_id) == []


REFUSALS = {
    "taken id": {"principal_id": "buyer"},
    "invalid id": {"principal_id": "two words"},
    "reserved id": {"principal_id": "anonymous"},
    "missing name": {"name": ""},
    "non-numeric gam advertiser": {"gam_advertiser_id": "abc"},
}


@pytest.mark.parametrize("fields", REFUSALS.values(), ids=REFUSALS)
def test_refused_create_is_audited_as_failure(creation_env, fields):
    env, client, tenant = creation_env
    PrincipalFactory(tenant=tenant, principal_id="buyer")
    env.get_session()
    create(client, tenant.tenant_id, **{"principal_id": "new_buyer", **fields})
    assert audited_outcomes(env, tenant.tenant_id) == [False]


def test_unexpected_error_is_audited_as_failure(creation_env):
    env, client, tenant = creation_env
    with patch.object(PrincipalRepository, "issue", side_effect=RuntimeError("issue failed")):
        answer = operator_answer(client, create(client, tenant.tenant_id, principal_id="buyer"))
    assert answer[2] == [("error", "Error creating advertiser")]
    assert audited_outcomes(env, tenant.tenant_id) == [False]
    assert created_rows(env, tenant.tenant_id) == []


def test_same_id_is_available_in_another_tenant(creation_env):
    env, client, tenant = creation_env
    other = TenantFactory()
    env.get_session()
    for tenant_id in [tenant.tenant_id, other.tenant_id]:
        admin_auth_session(client, tenant_id)
        response = create(client, tenant_id, principal_id="buyer")
        assert response.status_code == 302
        (row,) = created_rows(env, tenant_id)
        assert row.principal_id == "buyer"


def test_legacy_post_without_an_id_keeps_automatic_generation(creation_env):
    env, client, tenant = creation_env
    response = create(client, tenant.tenant_id)
    assert response.status_code == 302
    (row,) = created_rows(env, tenant.tenant_id)
    assert row.principal_id.startswith("prin_")


def test_anonymous_post_cannot_create_a_principal(creation_env):
    env, client, tenant = creation_env
    with client.session_transaction() as session:
        session.clear()
    response = create(client, tenant.tenant_id, principal_id="buyer")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    assert created_rows(env, tenant.tenant_id) == []
