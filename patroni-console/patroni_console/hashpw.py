"""Хеш пароля для config.yaml: python -m patroni_console.hashpw"""
import getpass

from werkzeug.security import generate_password_hash

if __name__ == '__main__':
    p1 = getpass.getpass('Пароль: ')
    p2 = getpass.getpass('Ещё раз: ')
    if p1 != p2 or len(p1) < 12:
        raise SystemExit('пароли не совпадают или короче 12 символов')
    print(generate_password_hash(p1, method='scrypt'))
