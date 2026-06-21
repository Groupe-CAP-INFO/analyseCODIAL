"""
Wrapper GLPI REST API v10.
Gère l'authentification, la session, et les opérations CRUD de base.
"""
import urllib.request
import urllib.error
import urllib.parse
import base64
import json
from config import GLPI_URL, GLPI_APP_TOKEN, GLPI_LOGIN, GLPI_PASSWORD


class GlpiClient:
    def __init__(self):
        self.session_token = None

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *_):
        self.logout()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self):
        creds = base64.b64encode(f"{GLPI_LOGIN}:{GLPI_PASSWORD}".encode()).decode()
        data = self._request("GET", "/initSession", headers={
            "Authorization": "Basic " + creds,
        })
        self.session_token = data["session_token"]

    def logout(self):
        if self.session_token:
            try:
                self._request("GET", "/killSession")
            except Exception:
                pass
            self.session_token = None

    # ------------------------------------------------------------------
    # CRUD génériques
    # ------------------------------------------------------------------

    def get(self, endpoint, params=None):
        return self._request("GET", endpoint, params=params)

    def search(self, itemtype, criteria=None, forcedisplay=None, range_="0-999"):
        params = {"range": range_}
        if criteria:
            for i, c in enumerate(criteria):
                for k, v in c.items():
                    params[f"criteria[{i}][{k}]"] = str(v)
        if forcedisplay:
            for i, f in enumerate(forcedisplay):
                params[f"forcedisplay[{i}]"] = str(f)
        try:
            return self._request("GET", f"/search/{itemtype}", params=params)
        except urllib.error.HTTPError as e:
            if e.code == 206:
                return json.loads(e.read())
            raise

    def create(self, itemtype, fields):
        return self._request("POST", f"/{itemtype}", body={"input": fields})

    def update(self, itemtype, item_id, fields):
        return self._request("PUT", f"/{itemtype}/{item_id}", body={"input": fields})

    def delete(self, itemtype, item_id, purge=False):
        params = {"force_purge": "1"} if purge else {}
        return self._request("DELETE", f"/{itemtype}/{item_id}", params=params)

    def get_by_id(self, itemtype, item_id):
        return self._request("GET", f"/{itemtype}/{item_id}")

    # ------------------------------------------------------------------
    # Helpers métier
    # ------------------------------------------------------------------

    def find_entity_by_name(self, name):
        result = self.search("Entity", criteria=[
            {"field": "1", "searchtype": "equals", "value": name}
        ])
        data = result.get("data", [])
        return data[0] if data else None

    def find_user_by_login(self, login):
        result = self.search("User", criteria=[
            {"field": "1", "searchtype": "equals", "value": login}
        ])
        data = result.get("data", [])
        return data[0] if data else None

    def list_entities(self, range_="0-9999"):
        return self.search("Entity", range_=range_)

    def list_profiles(self):
        return self.search("Profile", range_="0-999")

    # ------------------------------------------------------------------
    # HTTP interne
    # ------------------------------------------------------------------

    def _request(self, method, endpoint, params=None, body=None, headers=None):
        url = GLPI_URL.rstrip("/") + endpoint
        if params:
            url += "?" + urllib.parse.urlencode(params)

        data = json.dumps(body).encode() if body else None
        h = {
            "App-Token": GLPI_APP_TOKEN,
            "Content-Type": "application/json",
        }
        if self.session_token:
            h["Session-Token"] = self.session_token
        if headers:
            h.update(headers)

        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read()
                return json.loads(content) if content else {}
        except urllib.error.HTTPError as e:
            body_err = e.read().decode()
            raise urllib.error.HTTPError(
                e.url, e.code,
                f"{e.reason} — {body_err[:300]}",
                e.headers, None
            )
