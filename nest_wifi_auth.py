"""Google Wifi authentication with a hidden one-time cookie prompt."""

from __future__ import annotations

import re
from urllib.parse import unquote

import gpsoauth
import requests

from nest_wifi_keychain import MonitorError

AUTH_URL = "https://android.clients.google.com/auth"


def normalize_oauth_cookie(pasted: str) -> str:
    """Validate the EmbeddedSetup cookie without echoing bad input."""
    value = pasted.strip()
    if value.startswith("oauth_token="):
        value = value.split("=", 1)[1].split(";", 1)[0].strip()
    value = value.strip("\"'").strip()
    if value.lower().startswith("oauth2_4%2f"):
        value = unquote(value)
    if not re.fullmatch(r"oauth2_4/[^\s\"';]+", value):
        raise MonitorError("Expected a complete oauth_token cookie value.")
    return value


def google_auth(credentials: dict[str, str], cookie: str | None = None) -> str:
    """Exchange an existing Keychain token or a one-time sign-in cookie."""
    fields = dict(
        accountType="HOSTED_OR_GOOGLE",
        Email=credentials["email"],
        androidId=credentials["android_id"],
        has_permission=1,
        source="android",
        device_country="us",
        operatorCountry="us",
        lang="en",
        sdk_version=17,
        google_play_services_version=240913000,
    )
    if cookie:
        signature = "38918a453d07199354f8b19af05ec6562ced5788"
        fields.update(
            Token=cookie,
            ACCESS_TOKEN=1,
            add_account=1,
            service="ac2dm",
            client_sig=signature,
            callerSig=signature,
            droidguard_results="dummy123",
        )
    else:
        fields.update(
            EncryptedPasswd=credentials["master_token"],
            service="oauth2:https://www.googleapis.com/auth/accesspoints",
            app="com.google.android.apps.chromecast.app",
            client_sig="24bb24c05e47e0aefa68a58a766179d9b613a600",
        )
    try:
        with requests.Session() as session:
            session.trust_env = False
            session.mount(AUTH_URL, gpsoauth.AuthHTTPAdapter())
            response = session.post(
                AUTH_URL,
                data=fields,
                timeout=(10, 30),
                allow_redirects=False,
                headers={"User-Agent": "GoogleAuth/1.4", "Accept-Encoding": "identity"},
            )
            if response.status_code not in (200, 401, 403):
                raise MonitorError(f"Google authentication HTTP {response.status_code}")
            result = gpsoauth.google.parse_auth_response(response.text)
    except requests.RequestException:
        raise MonitorError("Cannot reach Google authentication.") from None
    wanted = "Token" if cookie else "Auth"
    if result.get("Error") or not result.get(wanted):
        raise MonitorError("Google rejected authentication; run auth interactively.")
    return result[wanted]
