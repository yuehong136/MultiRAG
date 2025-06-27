#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import json
import urllib.parse
import requests
from .oauth import OAuthClient, UserInfo


class FeishuOAuthClient(OAuthClient):
    def __init__(self, config):
        """
        Initialize the FeishuOAuthClient with the provider's configuration.
        """
        config.update({
            "authorization_url": "https://open.feishu.cn/open-apis/authen/v1/index",
            "token_url": config.get("user_access_token_url", "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token"),
            "userinfo_url": "https://open.feishu.cn/open-apis/authen/v1/user_info",
            "scope": "contact:user.email:readonly"
        })
        super().__init__(config)
        self.app_id = config.get("app_id")
        self.app_secret = config.get("app_secret")
        self.app_access_token_url = config.get("app_access_token_url", "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal")
        self.grant_type = config.get("grant_type", "authorization_code")


    def get_authorization_url(self, state=None):
        """
        Generate the authorization URL for Feishu login.
        """
        params = {
            "app_id": self.app_id,
            "redirect_uri": self.redirect_uri,
        }
        if state:
            params["state"] = state
        authorization_url = f"{self.authorization_url}?{urllib.parse.urlencode(params)}"
        return authorization_url


    def get_app_access_token(self):
        """
        Get Feishu app access token.
        """
        try:
            payload = {
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            }
            response = requests.post(
                self.app_access_token_url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=self.http_request_timeout
            )
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 0:
                raise ValueError(f"Failed to get app access token: {result}")
            return result.get("app_access_token")
        except requests.exceptions.RequestException as e:
            raise ValueError(f"Failed to get app access token: {e}")


    def exchange_code_for_token(self, code):
        """
        Exchange authorization code for access token.
        """
        try:
            app_access_token = self.get_app_access_token()
            payload = {
                "grant_type": self.grant_type,
                "code": code,
            }
            response = requests.post(
                self.token_url,
                data=json.dumps(payload),
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {app_access_token}"
                },
                timeout=self.http_request_timeout
            )
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 0:
                raise ValueError(f"Failed to exchange code for token: {result}")
            return result.get("data", {})
        except requests.exceptions.RequestException as e:
            raise ValueError(f"Failed to exchange authorization code for token: {e}")


    def fetch_user_info(self, access_token, **kwargs):
        """
        Fetch Feishu user info.
        """
        try:
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {access_token}"
            }
            response = requests.get(self.userinfo_url, headers=headers, timeout=self.http_request_timeout)
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 0:
                raise ValueError(f"Failed to fetch user info: {result}")
            user_info = result.get("data", {})
            return self.normalize_user_info(user_info)
        except requests.exceptions.RequestException as e:
            raise ValueError(f"Failed to fetch Feishu user info: {e}")


    def normalize_user_info(self, user_info):
        email = user_info.get("email")
        if email == "":
            email = None
        username = user_info.get("en_name", user_info.get("name", str(email).split("@")[0] if email else ""))
        nickname = user_info.get("name", user_info.get("en_name", username))
        avatar_url = user_info.get("avatar_url", "")
        return UserInfo(email=email, username=username, nickname=nickname, avatar_url=avatar_url)
