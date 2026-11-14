"""快照路由的权限边界：viewer 只读、analyst/admin 可展开续跑、裁决复用缺陷整改权限。

不启动 HTTP 服务，直接检查路由依赖闭包里的角色白名单，保证 403 由后端真实
依赖强制，而不是前端隐藏按钮。
"""
from __future__ import annotations

from src.constants.roles import (
    CRAWL_CREATE_ROLES,
    DEFECT_REVIEW_ROLES,
    READ_ROLES,
)
from src.routes.snapshots import router

WRITE_ACTIONS = {"create_batch", "resume_batch"}
REVIEW_ACTIONS = {"resolve_review"}
READ_ACTIONS = {"list_batches", "get_batch", "list_reviews"}


def _roles_of_route(route) -> set[str]:
    assert route.dependencies, route.name
    dependency = route.dependencies[0].call
    cells = [cell.cell_contents for cell in (dependency.__closure__ or ())]
    for value in cells:
        if isinstance(value, tuple) and value and all(isinstance(item, str) for item in value):
            return set(value)
    raise AssertionError(f"route {route.name} 没有角色白名单闭包")


def _route_roles() -> dict[str, set[str]]:
    return {route.name: _roles_of_route(route) for route in router.routes}


def test_read_endpoints_allow_all_three_roles():
    roles = _route_roles()
    for action in READ_ACTIONS:
        assert set(READ_ROLES).issubset(roles[action]), (action, roles[action])


def test_expand_and_resume_require_crawl_roles():
    roles = _route_roles()
    for action in WRITE_ACTIONS:
        assert roles[action] == set(CRAWL_CREATE_ROLES), (action, roles[action])
        assert "viewer" not in roles[action]


def test_review_requires_defect_review_roles():
    roles = _route_roles()
    for action in REVIEW_ACTIONS:
        assert roles[action] == set(DEFECT_REVIEW_ROLES), (action, roles[action])
        assert "viewer" not in roles[action]
