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
```

## 2. Подключение к postgresql
```bash
sudo -i -u postgres psql
```

## 3. Работа с транзакциями: создание таблицы shipments
### Действия в первой сессии
```sql
\set AUTOCOMMIT off
BEGIN;

create table shipments(
    id serial primary key, 
    product_name text, 
    quantity int, 
    destination text
);

insert into shipments(product_name, quantity, destination) 
values('bananas', 1000, 'Europe');

insert into shipments(product_name, quantity, destination) 
values('coffee', 500, 'USA');

COMMIT;
```
### Пояснения
```sql
\set AUTOCOMMIT off   -- отключаем автоматическую фиксацию транзакции
BEGIN;                -- начало транзакции
COMMIT;               -- фиксация изменений
```
