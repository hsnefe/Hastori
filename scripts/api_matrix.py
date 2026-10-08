"""The authorization matrix: every endpoint, every demo user, the status code each must get.

One table, two users: `services/api/tests` run it against the application in process, and
`make e2e` runs it against the live stack (so what the tests proved is checked again where the
code really runs). It only talks HTTP, and finds the ids it needs through the API itself.

The point of the matrix is tenant isolation: an object of another site must answer 404 (not 403,
not 200), a role that may not do something 403, and no token 401, on every endpoint, for every
user. A missing filter on one endpoint shows up as one wrong cell.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# actor -> (e-mail key in the demo seed); the passwords come from the caller
ACTORS = ("admin", "izmir_admin", "izmir_viewer", "antalya_admin", "antalya_viewer")
ZERO = "00000000-0000-0000-0000-00000000dead"  # a well-formed id that exists nowhere


@dataclass
class Ids:
    """What the matrix aims at; found through the API with the system admin's token."""

    izmir_site: str
    antalya_site: str
    izmir_device: str  # a compressor in İzmir
    antalya_device: str
    izmir_rule: str
    antalya_rule: str
    izmir_alarm: str | None = None
    antalya_alarm: str | None = None
    izmir_user: str | None = None


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Case:
    name: str
    method: str
    path: str
    expect: dict[str, int]  # actor -> status; a missing actor is not tested
    body: Any = None
    needs: str | None = None  # an Ids field that must be set for the case to apply
    check: Callable[[str, Any], str | None] | None = None  # (actor, response json) -> problem


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append(Result(name, ok, detail))

    @property
    def failures(self) -> list[Result]:
        return [r for r in self.results if not r.ok]


def _both(i: dict[str, int], a: dict[str, int]) -> dict[str, int]:
    return {**i, **a}


# Who sees which site: the system admin both, the others their own.
IZMIR_ONLY = {
    "admin": 200,
    "izmir_admin": 200,
    "izmir_viewer": 200,
    "antalya_admin": 404,
    "antalya_viewer": 404,
}
ANTALYA_ONLY = {
    "admin": 200,
    "izmir_admin": 404,
    "izmir_viewer": 404,
    "antalya_admin": 200,
    "antalya_viewer": 200,
}
EVERYONE_SIGNED_IN = {a: 200 for a in ACTORS}
ADMINS_ONLY = {
    "admin": 200,
    "izmir_admin": 200,
    "izmir_viewer": 403,
    "antalya_admin": 200,
    "antalya_viewer": 403,
}
SYSTEM_ADMIN_ONLY = {
    "admin": 200,
    "izmir_admin": 403,
    "izmir_viewer": 403,
    "antalya_admin": 403,
    "antalya_viewer": 403,
}


def _sites_seen(actor: str, body: Any) -> str | None:
    names = sorted(s["name"] for s in body)
    want = {
        "admin": ["Antalya Otel", "İzmir Fabrika"],
        "izmir_admin": ["İzmir Fabrika"],
        "izmir_viewer": ["İzmir Fabrika"],
        "antalya_admin": ["Antalya Otel"],
        "antalya_viewer": ["Antalya Otel"],
    }[actor]
    return None if names == want else f"sees {names}, should see {want}"


def _is_ticket(actor: str, body: Any) -> str | None:
    ok = (
        isinstance(body.get("ticket"), str) and len(body["ticket"]) >= 40 and body["expires_in"] > 0
    )
    return None if ok else f"not a ticket: {str(body)[:80]}"


def _only_own_sites(ids: Ids) -> Callable[[str, Any], str | None]:
    def check(actor: str, body: Any) -> str | None:
        allowed = {
            "admin": {ids.izmir_site, ids.antalya_site},
            "izmir_admin": {ids.izmir_site},
            "izmir_viewer": {ids.izmir_site},
            "antalya_admin": {ids.antalya_site},
            "antalya_viewer": {ids.antalya_site},
        }[actor]
        leaked = [i for i in body["items"] if i["site_id"] not in allowed]
        return f"{len(leaked)} items of other sites" if leaked else None

    return check


def cases(ids: Ids) -> list[Case]:
    metric = "?metric=temperature_c"
    out = [
        Case("GET /sites", "GET", "/sites", EVERYONE_SIGNED_IN, check=_sites_seen),
        Case("GET /auth/me", "GET", "/auth/me", EVERYONE_SIGNED_IN),
        Case("WebSocket ticket", "POST", "/ws-ticket", EVERYONE_SIGNED_IN, check=_is_ticket),
        # a site, its devices, its consumption
        Case("devices of İzmir", "GET", f"/sites/{ids.izmir_site}/devices", IZMIR_ONLY),
        Case("devices of Antalya", "GET", f"/sites/{ids.antalya_site}/devices", ANTALYA_ONLY),
        Case("consumption İzmir", "GET", f"/sites/{ids.izmir_site}/consumption/daily", IZMIR_ONLY),
        Case(
            "consumption Antalya",
            "GET",
            f"/sites/{ids.antalya_site}/consumption/daily",
            ANTALYA_ONLY,
        ),
        Case("unknown site", "GET", f"/sites/{ZERO}/devices", {a: 404 for a in ACTORS}),
        # measurements of a device
        Case(
            "measurements İzmir",
            "GET",
            f"/devices/{ids.izmir_device}/measurements{metric}",
            IZMIR_ONLY,
        ),
        Case(
            "measurements Antalya",
            "GET",
            f"/devices/{ids.antalya_device}/measurements{metric}",
            ANTALYA_ONLY,
        ),
        Case(
            "unknown device",
            "GET",
            f"/devices/{ZERO}/measurements{metric}",
            {a: 404 for a in ACTORS},
        ),
        # alarms
        Case("alarm list", "GET", "/alarms", EVERYONE_SIGNED_IN, check=_only_own_sites(ids)),
        Case(
            "alarm list of İzmir",
            "GET",
            f"/alarms?site_id={ids.izmir_site}",
            IZMIR_ONLY,
            check=_only_own_sites(ids),
        ),
        Case(
            "alarm list of Antalya",
            "GET",
            f"/alarms?site_id={ids.antalya_site}",
            ANTALYA_ONLY,
            check=_only_own_sites(ids),
        ),
        Case("unknown alarm", "GET", f"/alarms/{ZERO}", {a: 404 for a in ACTORS}),
        Case(
            "ack unknown alarm",
            "POST",
            f"/alarms/{ZERO}/ack",
            _both(
                {"izmir_viewer": 403, "antalya_viewer": 403},
                {"admin": 404, "izmir_admin": 404, "antalya_admin": 404},
            ),
        ),
        # alarm rules: admins only, within their sites
        Case("rule list", "GET", "/alarm-rules", ADMINS_ONLY, check=_only_own_sites(ids)),
        Case(
            "rule of İzmir",
            "GET",
            f"/alarm-rules/{ids.izmir_rule}",
            _both(ADMINS_ONLY, {"antalya_admin": 404}),
        ),
        Case(
            "rule of Antalya",
            "GET",
            f"/alarm-rules/{ids.antalya_rule}",
            _both(ADMINS_ONLY, {"izmir_admin": 404}),
        ),
        Case(
            "unknown rule",
            "GET",
            f"/alarm-rules/{ZERO}",
            _both(
                {"izmir_viewer": 403, "antalya_viewer": 403},
                {"admin": 404, "izmir_admin": 404, "antalya_admin": 404},
            ),
        ),
        Case(
            "disable rule of İzmir",
            "DELETE",
            f"/alarm-rules/{ids.izmir_rule}",
            {"izmir_viewer": 403, "antalya_viewer": 403, "antalya_admin": 404},
        ),
        Case(
            "disable rule of Antalya",
            "DELETE",
            f"/alarm-rules/{ids.antalya_rule}",
            {"izmir_viewer": 403, "antalya_viewer": 403, "izmir_admin": 404},
        ),
        Case(
            "replace rule of İzmir",
            "PUT",
            f"/alarm-rules/{ids.izmir_rule}",
            {"izmir_viewer": 403, "antalya_viewer": 403, "antalya_admin": 404},
            body=_rule_body(),
        ),
        Case(
            "new rule on a device of İzmir",
            "POST",
            "/alarm-rules",
            {"izmir_viewer": 403, "antalya_viewer": 403, "antalya_admin": 404},
            body={**_rule_body(), "device_id": ids.izmir_device},
        ),
        Case(
            "new rule on a device of Antalya",
            "POST",
            "/alarm-rules",
            {"izmir_viewer": 403, "antalya_viewer": 403, "izmir_admin": 404},
            body={**_rule_body(), "device_id": ids.antalya_device},
        ),
        # users and sites: the system admin only
        Case("user list", "GET", "/users", SYSTEM_ADMIN_ONLY),
        Case(
            "new user",
            "POST",
            "/users",
            {a: 403 for a in ACTORS if a != "admin"},
            body={"email": "x@y.zz", "password": "a-long-password", "role": "viewer"},
        ),
        Case(
            "new site",
            "POST",
            "/sites",
            {a: 403 for a in ACTORS if a != "admin"},
            body={"name": "Nowhere"},
        ),
        Case(
            "change a site",
            "PATCH",
            f"/sites/{ids.izmir_site}",
            {a: 403 for a in ACTORS if a != "admin"},
            body={"city": "x"},
        ),
    ]
    if ids.izmir_alarm:
        out += [
            Case("alarm of İzmir", "GET", f"/alarms/{ids.izmir_alarm}", IZMIR_ONLY),
            Case(
                "ack alarm of İzmir",
                "POST",
                f"/alarms/{ids.izmir_alarm}/ack",
                {"izmir_viewer": 403, "antalya_viewer": 403, "antalya_admin": 404},
            ),
        ]
    if ids.antalya_alarm:
        out += [
            Case("alarm of Antalya", "GET", f"/alarms/{ids.antalya_alarm}", ANTALYA_ONLY),
            Case(
                "ack alarm of Antalya",
                "POST",
                f"/alarms/{ids.antalya_alarm}/ack",
                {"izmir_viewer": 403, "antalya_viewer": 403, "izmir_admin": 404},
            ),
        ]
    if ids.izmir_user:
        out += [
            Case("one user", "GET", f"/users/{ids.izmir_user}", SYSTEM_ADMIN_ONLY),
            Case(
                "change a user",
                "PATCH",
                f"/users/{ids.izmir_user}",
                {a: 403 for a in ACTORS if a != "admin"},
                body={"role": "viewer"},
            ),
        ]
    return out


def _rule_body() -> dict[str, Any]:
    return {
        "name": "matrix probe",
        "metric": "temperature_c",
        "operator": ">",
        "threshold": 80,
        "clear_threshold": 75,
        "duration_s": 30,
        "severity": "warning",
    }


# Every endpoint that needs a token, asked without one (and with a bad one): always 401.
UNAUTHENTICATED: list[tuple[str, str]] = [
    ("GET", "/auth/me"),
    ("POST", "/ws-ticket"),
    ("GET", "/sites"),
    ("POST", "/sites"),
    ("PATCH", f"/sites/{ZERO}"),
    ("GET", f"/sites/{ZERO}/devices"),
    ("GET", f"/sites/{ZERO}/consumption/daily"),
    ("GET", f"/devices/{ZERO}/measurements?metric=current_a"),
    ("GET", "/alarms"),
    ("GET", f"/alarms/{ZERO}"),
    ("POST", f"/alarms/{ZERO}/ack"),
    ("GET", "/alarm-rules"),
    ("POST", "/alarm-rules"),
    ("GET", f"/alarm-rules/{ZERO}"),
    ("PUT", f"/alarm-rules/{ZERO}"),
    ("DELETE", f"/alarm-rules/{ZERO}"),
    ("GET", "/users"),
    ("POST", "/users"),
    ("GET", f"/users/{ZERO}"),
    ("PATCH", f"/users/{ZERO}"),
]

Request = Callable[..., Awaitable[Any]]


async def discover(admin: Any) -> Ids:
    """Find the ids the matrix needs, as the system admin."""
    sites = {s["name"]: s["id"] for s in (await admin.get("/sites")).json()}
    izmir, antalya = sites["İzmir Fabrika"], sites["Antalya Otel"]

    async def device(site: str, key_part: str) -> str:
        rows = (await admin.get(f"/sites/{site}/devices")).json()
        return str(next(d["id"] for d in rows if d["key"].endswith(key_part)))

    rules = (await admin.get("/alarm-rules?size=100")).json()["items"]
    alarms = (await admin.get("/alarms?size=100")).json()["items"]
    users = (await admin.get("/users?size=100")).json()["items"]
    return Ids(
        izmir_site=izmir,
        antalya_site=antalya,
        izmir_device=await device(izmir, "komp-1"),
        antalya_device=await device(antalya, "komp-1"),
        izmir_rule=next(
            r["id"] for r in rules if r["site_id"] == izmir and r["kind"] == "threshold"
        ),
        antalya_rule=next(r["id"] for r in rules if r["site_id"] == antalya),
        izmir_alarm=next((a["id"] for a in alarms if a["site_id"] == izmir), None),
        antalya_alarm=next((a["id"] for a in alarms if a["site_id"] == antalya), None),
        izmir_user=next(u["id"] for u in users if u["email"].startswith("izmir.izleyici")),
    )


async def run(clients: dict[str, Any], anonymous: Any, ids: Ids) -> Report:
    """Run the whole matrix. `clients`: an authenticated client per actor; `anonymous`: one with
    no token."""
    report = Report()
    for case in cases(ids):
        for actor, status in case.expect.items():
            r = await clients[actor].request(case.method, case.path, json=case.body)
            label = f"{case.name} as {actor}: {case.method} {case.path.split('?')[0]}"
            ok = r.status_code == status
            detail = "" if ok else f"expected {status}, got {r.status_code}: {r.text[:200]}"
            if ok and status == 200 and case.check is not None:
                problem = case.check(actor, r.json())
                ok, detail = problem is None, problem or ""
            if ok and status >= 400:
                shape = r.json()
                ok = (
                    isinstance(shape, dict) and set(shape) == {"error"} and "code" in shape["error"]
                )
                detail = "" if ok else f"error body is not the standard format: {r.text[:120]}"
            report.add(label, ok, detail)
    for method, path in UNAUTHENTICATED:
        for headers in ({}, {"Authorization": "Bearer not-a-token"}):
            r = await anonymous.request(method, path, headers=headers, json=None)
            ok = r.status_code == 401
            report.add(
                f"no/bad token: {method} {path.split('?')[0]}",
                ok,
                "" if ok else f"expected 401, got {r.status_code}",
            )
    # an id that is not an id is a client error, never a server error
    for path in (
        "/sites/not-a-uuid/devices",
        "/alarms/not-a-uuid",
        "/devices/1/measurements?metric=current_a",
    ):
        r = await clients["admin"].get(path)
        report.add(f"malformed id: GET {path}", r.status_code == 422, f"got {r.status_code}")
    return report
