from datetime import timedelta
from requests import RequestException

from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.helpers import (
    complete_social_login,
    render_authentication_error,
)
from allauth.socialaccount.models import SocialLogin, SocialToken
from allauth.socialaccount.providers.base import ProviderException
from allauth.socialaccount.providers.base.constants import AuthError
from allauth.socialaccount.providers.base.mixins import OAuthLoginMixin
from allauth.socialaccount.providers.oauth2.client import (
    OAuth2Client,
    OAuth2Error,
)
from allauth.utils import build_absolute_uri, get_request_param


class OAuth2Adapter(object):
    expires_in_key = "expires_in"
    client_class = OAuth2Client
    supports_state = True
    redirect_uri_protocol = None
    access_token_method = "POST"
    login_cancelled_error = "access_denied"
    scope_delimiter = " "
    basic_auth = False
    headers = None

    def __init__(self, request):
        self.request = request
        self.did_fetch_access_token = False

    def get_provider(self):
        return get_adapter(self.request).get_provider(
            self.request, provider=self.provider_id
        )

    def complete_login(self, request, app, access_token, **kwargs):
        """
        Returns a SocialLogin instance
        """
        raise NotImplementedError

    def get_callback_url(self, request, app):
        callback_url = reverse(self.provider_id + "_callback")
        protocol = self.redirect_uri_protocol
        return build_absolute_uri(request, callback_url, protocol)

    def parse_token(self, data):
        token = SocialToken(token=data["access_token"])
        token.token_secret = data.get("refresh_token", "")
        expires_in = data.get(self.expires_in_key, None)
        if expires_in:
            token.expires_at = timezone.now() + timedelta(seconds=int(expires_in))
        return token

    def get_access_token_data(self, request, app, client):
        code = get_request_param(self.request, "code")
        pkce_code_verifier = request.session.pop("pkce_code_verifier", None)
        data = client.get_access_token(code, pkce_code_verifier=pkce_code_verifier)
        self.did_fetch_access_token = True
        return data

    def get_client(self, request, app):
        callback_url = self.get_callback_url(request, app)
        client = self.client_class(
            self.request,
            app.client_id,
            app.secret,
            self.access_token_method,
            self.access_token_url,
            callback_url,
            scope_delimiter=self.scope_delimiter,
            headers=self.headers,
            basic_auth=self.basic_auth,
        )
        return client


class OAuth2View(object):
    @classmethod
    def adapter_view(cls, adapter):
        def view(request, *args, **kwargs):
            self = cls()
            self.request = request
            if not isinstance(adapter, OAuth2Adapter):
                self.adapter = adapter(request)
            else:
                self.adapter = adapter
            try:
                return self.dispatch(request, *args, **kwargs)
            except ImmediateHttpResponse as e:
                return e.response

        return view


class OAuth2LoginView(OAuthLoginMixin, OAuth2View):
    def login(self, request, *args, **kwargs):
        provider = self.adapter.get_provider()
        return provider.redirect_from_request(request)


class OAuth2CallbackView(OAuth2View):
    def dispatch(self, request, *args, **kwargs):
        provider = self.adapter.get_provider()
        if "error" in request.GET or "code" not in request.GET:
            # Distinguish cancel from error
            auth_error = request.GET.get("error", None)
            if auth_error == self.adapter.login_cancelled_error:
                error = AuthError.CANCELLED
            else:
                error = AuthError.UNKNOWN
            return render_authentication_error(
                request,
                provider,
                error=error,
                extra_context={
                    "callback_view": self,
                },
            )
        app = provider.app
        client = self.adapter.get_client(self.request, app)

        try:
            access_token = self.adapter.get_access_token_data(request, app, client)
            token = self.adapter.parse_token(access_token)
            if app.pk:
                token.app = app
            login = self.adapter.complete_login(
                request, app, token, response=access_token
            )
            login.token = token
            if self.adapter.supports_state:
                login.state = SocialLogin.verify_and_unstash_state(
                    request, get_request_param(request, "state")
                )
            else:
                login.state = SocialLogin.unstash_state(request)

            return complete_social_login(request, login)
        except (
            PermissionDenied,
            OAuth2Error,
            RequestException,
            ProviderException,
        ) as e:
            return render_authentication_error(request, provider, exception=e)
