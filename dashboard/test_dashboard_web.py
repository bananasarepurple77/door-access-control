"""Flask route tests; run after installing requirements-dashboard.txt.
Uses a temporary sample database. Never opens the live events.db.
"""
import importlib.util
import re
import unittest

import dashboard_app as d
import test_dashboard as test_helpers

HAS_FLASK = importlib.util.find_spec("flask") is not None


@unittest.skipUnless(HAS_FLASK, "Install requirements-dashboard.txt to test Flask routes")
class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hashed = d.password_hash("test-only-password-123")

    def setUp(self):
        self.fixture = test_helpers.DashboardTests()
        self.fixture.setUp()
        self.settings = {"username":"admin", "password_hash":self.hashed,
                         "secret_key":"test-only-secret-not-for-deployment-"*2,
                         "timezone":"Australia/Sydney", "db_path":str(self.fixture.db)}
        self.app = d.create_app(self.settings)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.fixture.tearDown()

    def token(self):
        html = self.client.get("/login").get_data(as_text=True)
        return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    def login(self):
        return self.client.post("/login", data={"csrf":self.token(),"username":"admin", "password":"test-only-password-123"})

    def test_login_page(self):
        self.assertEqual(self.client.get("/login").status_code,200)

    def test_anonymous_api_denied(self):
        self.assertEqual(self.client.get("/api/events").status_code,401)

    def test_anonymous_html_redirects(self):
        self.assertEqual(self.client.get("/").status_code,302)
        self.assertEqual(self.client.get("/export.csv").status_code,302)

    def test_csrf_required(self):
        self.assertEqual(self.client.post("/login",data={"username":"admin","password":"test-only-password-123"}).status_code,400)

    def test_unicode_csrf_does_not_crash(self):
        self.token()
        self.assertEqual(self.client.post("/login",data={"csrf":"\u1234"}).status_code,400)

    def test_successful_login_and_data(self):
        self.assertEqual(self.login().status_code,302)
        self.assertEqual(self.client.get("/").status_code,200)
        result=self.client.get("/api/events")
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json["stats"]["total"],5)
        self.assertNotIn("snapshot", result.json["events"][0])

    def test_filters_and_errors(self):
        self.login()
        self.assertEqual(self.client.get("/api/events?decision=DENIED").json["stats"]["total"],2)
        self.assertEqual(self.client.get("/api/events?page=no").status_code,400)

    def test_export(self):
        self.login()
        result=self.client.get("/export.csv?decision=DENIED")
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.mimetype,"text/csv")
        self.assertIn("attachment",result.headers["Content-Disposition"])
        self.assertNotIn("secret.jpg",result.get_data(as_text=True))

    def test_logout(self):
        self.login()
        with self.client.session_transaction() as session:
            csrf=session["csrf"]
        self.assertEqual(self.client.post("/logout",data={"csrf":csrf}).status_code,302)
        self.assertEqual(self.client.get("/api/events").status_code,401)

    def test_security_headers(self):
        response=self.client.get("/login")
        self.assertEqual(response.headers["X-Frame-Options"],"DENY")
        self.assertEqual(response.headers["Cache-Control"],"no-store")
        self.assertIn("script-src 'self'",response.headers["Content-Security-Policy"])

    def test_expired_session(self):
        import time
        self.login()
        with self.client.session_transaction() as session:
            session["login_at"]=time.time()-d.SESSION_SECONDS-10
        self.assertEqual(self.client.get("/api/events").status_code,401)

    def test_rate_limit(self):
        token=self.token()
        for _ in range(5):
            response=self.client.post("/login",data={"csrf":token,"username":"admin","password":"wrong"})
            self.assertEqual(response.status_code,401)
        self.assertEqual(self.client.post("/login",data={"csrf":token,"username":"admin","password":"wrong"}).status_code,429)


if __name__ == "__main__":
    unittest.main(verbosity=2)
