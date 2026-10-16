"""Хеш пароля для config.yaml: patroni-console-hashpw (или python -m patroni_console.hashpw)."""
import getpass

from werkzeug.security import generate_password_hash


def main():
    p1 = getpass.getpass('Пароль: ')
    p2 = getpass.getpass('Ещё раз: ')
    if p1 != p2 or not 12 <= len(p1) <= 256:
        raise SystemExit('пароли не совпадают или длина не от 12 до 256 символов')
    print(generate_password_hash(p1, method='scrypt'))


if __name__ == '__main__':
    main()
