"""Check actual HTTP responses using the same declarations as the published contract."""

import re
from functools import lru_cache

from pydantic import TypeAdapter

from selark.contracts import dashboard as d
from selark.contracts.openapi import DASHBOARD_OPERATIONS, dashboard_document


@lru_cache
def adapter(name):
    lists = {
        "ProviderList": d.Provider,
        "ConnectionList": d.ManagedConnection,
        "ApplicationList": d.ManagedApplication,
    }
    return TypeAdapter(list[lists[name]] if name in lists else getattr(d, name))


@lru_cache
def specification():
    return dashboard_document()


def dashboard_response(response):
    path = response.request.url.path
    for method, template, _identity, _summary, _request, result in DASHBOARD_OPERATIONS:
        if response.request.method.lower() != method or not re.fullmatch(
            template.replace("{identity}", "[^/]+"), path
        ):
            continue
        operation = specification()["paths"][template][method]
        assert str(response.status_code) in operation["responses"], (path, response.status_code)
        value = response.json()
        name = "DashboardError" if response.status_code >= 400 else result
        if name.endswith("List"):
            assert set(value) == {"items"}
            value = value["items"]
        adapter(name).validate_python(value)
        return response
    raise AssertionError(f"Undocumented response: {response.request.method} {path}")
