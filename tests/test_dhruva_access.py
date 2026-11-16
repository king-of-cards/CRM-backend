"""Dhruva logins (role "dhruva") may only use the Dhruva leads section: every other endpoint
answers 403. Walks the whole route table, so a new endpoint can't forget the rule.

In-memory SQLite (see test_crm_leads_api.ApiTestBase). Run:

    python -m unittest discover -s tests -t . -v
"""
import re
import unittest

from sqlmodel import Session

from app.main import app
from app.models.user import User
from tests.test_crm_leads_api import API, ApiTestBase

# Reachable by every login (Dhruva too): the login itself and reading your own user
OPEN_TO_ALL = {("GET", f"{API}/auth/me"), ("POST", f"{API}/auth/login"), ("POST", f"{API}/auth/refresh"),
               ("POST", f"{API}/auth/logout"), ("GET", f"{API}/users/{{user_id}}")}


def is_dhruva_route(path: str) -> bool:
    return "/dhruva" in path


def is_public(path: str) -> bool:
    return path.startswith(f"{API}/health")          # health checks: no login at all


class DhruvaAccessTest(ApiTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with Session(self.engine, expire_on_commit=False) as s:
            self.users["dhruva"] = User(name="Dhruva One", email="dhruva1@example.com", hashed_password="x", role="dhruva")
            s.add(self.users["dhruva"])
            s.commit()
            s.refresh(self.users["dhruva"])

    def routes(self):
        """Every API operation (from the OpenAPI schema: the routers are nested)."""
        app.openapi_schema = None
        for path, operations in app.openapi()["paths"].items():
            if path.startswith(API):
                for method in operations:
                    if method.upper() not in ("HEAD", "OPTIONS"):
                        yield method.upper(), path

    def test_every_other_endpoint_answers_403(self):
        checked = 0
        with self.logged_in("dhruva"):
            for method, path in self.routes():
                if (method, path) in OPEN_TO_ALL or is_dhruva_route(path) or is_public(path):
                    continue
                url = re.sub(r"\{[^}]+\}", "1", path)
                resp = self.client.request(method, url)
                self.assertEqual(resp.status_code, 403, (method, path, resp.text))
                checked += 1
        self.assertGreater(checked, 40)          # the 4 sources, Vendor -> KOC, dashboards, admin pages

    def test_messages_and_what_stays_open(self):
        with self.logged_in("dhruva"):
            for url in (f"{API}/whatsapp-leads", f"{API}/vendor-to-koc/leads", f"{API}/analytics/overview",
                        f"{API}/analytics/vendor-to-koc", f"{API}/vendor-to-koc/leads/template"):
                resp = self.client.get(url)
                self.assertEqual((resp.status_code, resp.json()["detail"]),
                                 (403, "Dhruva logins can only use the Dhruva leads section."), url)
            me = self.client.get(f"{API}/auth/me").json()
            self.assertEqual((me["role"], me["vendor_id"], me["vendor"]), ("dhruva", None, None))
            self.assertEqual(self.client.get(f"{API}/users/{self.users['dhruva'].id}").status_code, 200)   # self
            self.assertEqual(self.client.get(f"{API}/users/{self.users['admin'].id}").status_code, 404)    # others

    def test_admins_create_dhruva_logins(self):
        body = {"name": "Dhruva Two", "email": "dhruva2@example.com", "password": "password123", "role": "dhruva"}
        resp = self.client.post(f"{API}/users", json=body)
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual((resp.json()["role"], resp.json()["vendor_id"]), ("dhruva", None))
        resp = self.client.post(f"{API}/users", json={**body, "email": "dhruva3@example.com", "vendor_id": 1})
        self.assertEqual(resp.status_code, 422)                                   # no vendor for Dhruva logins
        with self.logged_in("dhruva"):
            self.assertEqual(self.client.post(f"{API}/users", json={**body, "email": "x@example.com"}).status_code, 403)
        # Several Dhruva logins are fine
        resp = self.client.post(f"{API}/users", json={**body, "email": "dhruva4@example.com"})
        self.assertEqual(resp.status_code, 201)


if __name__ == "__main__":
    unittest.main()
