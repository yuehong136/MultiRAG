import argparse
import base64
import sys
from cmd import Cmd
from typing import Any

import getpass
import warnings
from Cryptodome.PublicKey import RSA
from Cryptodome.Cipher import PKCS1_v1_5 as Cipher_pkcs1_v1_5
from lark import Lark, Tree
from parser import GRAMMAR, MultiRAGCLITransformer
from http_client import HttpClient
from multirag_client import MultiRAGClient, run_command, show_help
from user import login_user

warnings.filterwarnings("ignore", category=getpass.GetPassWarning)


def encrypt(input_string):
    pub = '-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEArq9XTUSeYr2+N1h3Afl/z8Dse/2yD0ZGrKwx+EEEcdsBLca9Ynmx3nIB5obmLlSfmskLpBo0UACBmB5rEjBp2Q2f3AG3Hjd4B+gNCG6BDaawuDlgANIhGnaTLrIqWrrcm4EMzJOnAOI1fgzJRsOOUEfaS318Eq9OVO3apEyCCt0lOQK6PuksduOjVxtltDav+guVAA068NrPYmRNabVKRNLJpL8w4D44sfth5RvZ3q9t+6RTArpEtc5sh5ChzvqPOzKGMXW83C95TxmXqpbK6olN4RevSfVjEAgCydH6HN6OhtOQEcnrU97r9H0iZOWwbw3pVrZiUkuRD1R56Wzs2wIDAQAB\n-----END PUBLIC KEY-----'
    pub_key = RSA.importKey(pub)
    cipher = Cipher_pkcs1_v1_5.new(pub_key)
    cipher_text = cipher.encrypt(base64.b64encode(input_string.encode('utf-8')))
    return base64.b64encode(cipher_text).decode("utf-8")


class MultiRAGCLI(Cmd):
    def __init__(self):
        super().__init__()
        self.parser = Lark(GRAMMAR, start='start', parser='lalr', transformer=MultiRAGCLITransformer())
        self.command_history = []
        self.account = "admin@datav.com"
        self.account_password: str = "admin"
        self.mode: str = "admin"
        self.multirag_client: MultiRAGClient | None = None

    intro = r"""Type "\h" for help, "\q" to quit."""
    prompt = "multirag> "

    def onecmd(self, command: str) -> bool:
        try:
            result = self.parse_command(command)

            if isinstance(result, dict):
                if 'type' in result and result.get('type') == 'empty':
                    return False

            self.execute_command(result)

            if isinstance(result, Tree):
                return False

            if result.get('type') == 'meta' and result.get('command') in ['q', 'quit', 'exit']:
                return True

        except KeyboardInterrupt:
            print("\nUse '\\q' to quit")
        except EOFError:
            print("\nGoodbye!")
            return True
        return False

    def emptyline(self) -> bool:
        return False

    def default(self, line: str) -> bool:
        return self.onecmd(line)

    def parse_command(self, command_str: str) -> dict[str, str]:
        if not command_str.strip():
            return {'type': 'empty'}

        self.command_history.append(command_str)

        try:
            result = self.parser.parse(command_str)
            return result
        except Exception as e:
            return {'type': 'error', 'message': f'Parse error: {str(e)}'}

    def verify_auth(self, arguments: dict[str, str | int], single_command: bool) -> bool:
        host = str(arguments["host"])
        port = int(arguments["port"])
        server_type = str(arguments.get("type", "admin"))
        self.account = str(arguments.get("username", "admin@datav.com"))
        self.mode = server_type

        http_client = HttpClient(host, port)

        if server_type == "admin":
            print("Attempt to access server for admin login")
        else:
            print("Attempt to access server for user login")

        attempt_count = 3
        if single_command:
            attempt_count = 1

        try_count = 0
        while True:
            try_count += 1
            if try_count > attempt_count:
                return False

            account_passwd = str(arguments["password"]) if single_command else getpass.getpass(
                f"password for {self.account}: "
            ).strip()
            try:
                encrypted_passwd = encrypt(account_passwd)
                token = login_user(http_client, server_type, self.account, encrypted_passwd)
                if not token.startswith("Bearer "):
                    token = f"Bearer {token}"
                http_client.login_token = token
                self.multirag_client = MultiRAGClient(http_client, server_type)
                print("Authentication successful.")
                return True
            except Exception as e:
                print(str(e))
                print("Can't access server for login (connection failed)")

    def execute_command(self, parsed_command: dict[str, Any]):
        command_dict: dict
        if isinstance(parsed_command, Tree):
            command_dict = parsed_command.children[0]
        else:
            if parsed_command["type"] == "error":
                print(f"Error: {parsed_command['message']}")
                return
            else:
                command_dict = parsed_command

        run_command(self.multirag_client, command_dict)

    def run_interactive(self, args: dict[str, Any]):
        if self.verify_auth(args, single_command=False):
            print(r"""
        __  ___      __  _ ____  ___   ______   ________    ____
       /  |/  /_  __/ /_(_) __ \/   | / ____/  / ____/ /   /  _/
      / /|_/ / / / / __/ / /_/ / /| |/ / __   / /   / /    / /
     / /  / / /_/ / /_/ / _, _/ ___ / /_/ /  / /___/ /____/ /
    /_/  /_/\__,_/\__/_/_/ |_/_/  |_\____/   \____/_____/___/
            """)
            self.cmdloop()

    def run_single_command(self, args: dict[str, Any]):
        if self.verify_auth(args, single_command=True):
            command: str = args['command']
            result = self.parse_command(command)
            self.execute_command(result)

    def parse_connection_args(self, args: list[str]) -> dict[str, Any]:
        parser = argparse.ArgumentParser(description="MultiRAG CLI Client", add_help=False)
        parser.add_argument("-h", "--host", default="localhost", help="Admin or MultiRAG service host")
        parser.add_argument("-p", "--port", type=int, default=8130, help="Admin or MultiRAG service port")
        parser.add_argument("-w", "--password", default="admin", type=str, help="Account password")
        parser.add_argument("-t", "--type", default="admin", type=str, help="CLI mode: admin or user")
        parser.add_argument(
            "-u", "--username", default=None,
            help="Username. In admin mode default is admin@datav.com, in user mode this is required."
        )
        parser.add_argument("command", nargs="?", help="Single command")

        try:
            parsed_args, remaining_args = parser.parse_known_args(args)
            mode = parsed_args.type.lower()
            username = parsed_args.username
            if mode == "admin":
                if username is None:
                    username = "admin@datav.com"
            else:
                if username is None:
                    print("Error: username (-u) is required in user mode")
                    return {"error": "Username required"}

            if remaining_args:
                command = remaining_args[0]
                return {
                    "host": parsed_args.host,
                    "port": parsed_args.port,
                    "password": parsed_args.password,
                    "type": mode,
                    "username": username,
                    "command": command,
                }
            return {
                "host": parsed_args.host,
                "port": parsed_args.port,
                "type": mode,
                "username": username,
            }
        except SystemExit:
            return {"error": "Invalid connection arguments"}


def main():
    cli = MultiRAGCLI()

    args = cli.parse_connection_args(sys.argv)
    if 'error' in args:
        print("Error: Invalid connection arguments")
        return

    if 'command' in args:
        # single command mode
        if 'password' not in args:
            print("Error: password is missing")
            return
        cli.run_single_command(args)
    else:
        # interactive mode
        cli.run_interactive(args)


if __name__ == '__main__':
    main()