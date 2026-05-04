# Домашнее задание N1: Работа с уровнями изоляции транзакций в PostgreSQL

## Информация о проекте
- **Название ВМ:** bananaflow-30081986
- **Дата выполнения:** 2026-05-04
- **Версия PostgreSQL:** 17

---

## 1. Установка PostgreSQL

```bash
dnf install postgresql18 postgresql-17-contrib
postgresql-17-setup initdb
systemctl enable postgresql-17.service --now
systemctl status postgresql-17.service
