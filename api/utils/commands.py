"""
@project: multirag
@Author：龙
@file： commands.py
@date：2024/7/26 13:55
@desc:
"""
import re

import click

from api.db.services import UserService


@click.command('reset-email', help='Reset the account email.')
@click.option('--email', prompt=True, help='The old email address of the account whose email you need to reset')
@click.option('--new-email', prompt=True, help='the new email.')
@click.option('--email-confirm', prompt=True, help='the new email confirm.')
def reset_email(email, new_email, email_confirm):
    if str(new_email).strip() != str(email_confirm).strip():
        click.echo(click.style('Sorry, new email and confirm email do not match.', fg='red'))
        return
    if str(new_email).strip() == str(email).strip():
        click.echo(click.style('Sorry, new email and old email are the same.', fg='red'))
        return
    user = UserService.query(email=email)
    if not user:
        click.echo(click.style(f'sorry. the account: [{email}] not exist .', fg='red'))
        return
    if not re.match(r"^[\w\._-]+@([\w_-]+\.)+[\w-]{2,4}$", new_email):
        click.echo(click.style(f'sorry. {new_email} is not a valid email. ', fg='red'))
        return
    new_user = UserService.query(email=new_email)
    if new_user:
        click.echo(click.style(f'sorry. the account: [{new_email}] is exist .', fg='red'))
        return
    user_dict = {
        'email': new_email
    }
    UserService.update_user(user[0].id,user_dict)
    click.echo(click.style('Congratulations!, email has been reset.', fg='green'))


