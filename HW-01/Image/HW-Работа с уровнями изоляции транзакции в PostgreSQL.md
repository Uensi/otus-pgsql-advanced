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
##### Действия в первой сессии
```sql
\set AUTOCOMMIT off
\echo :AUTOCOMMIT
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
![создание таблицы shipments](HW-01/image/create_table.png)
###### Пояснения
```sql
\set AUTOCOMMIT off   -- отключаем автоматическую фиксацию транзакции
BEGIN;                -- начало транзакции
COMMIT;               -- фиксация изменений
```

## 4. Эксперимент с уровнем изоляции READ COMMITTED
##### 4.1 Проверка текущего уровня изоляции (в обеих сессиях)
```sql
show transaction isolation level;
```
##### 4.2 Начало транзакций в обеих сессиях
```sql
BEGIN;
```
![создание таблицы shipments](HW-01/image/read_commited.png)

##### 4.3 В первой сессии добавляем новую запись
```sql
insert into shipments(product_name, quantity, destination) 
values('sugar', 300, 'Asia');
```
![создание таблицы shipments](HW-01/image/session1.png)

###### Команда COMMIT не выполнялась.

##### 4.4 Во второй сессии читаем все записи
```sql
select * from shipments;
```
![создание таблицы shipments](HW-01/image/session2.png)

###### Результат:
Новая запись о сахаре не видна. Видны только bananas и coffee.
Уровень READ COMMITTED защищает от "грязного чтения" — он показывает только те данные, которые были зафиксированы на момент выполнения команды SELECT. Поскольку первая транзакция ещё не завершена (COMMIT не было), её изменения считаются "грязными" и не показываются.

##### 4.5 Фиксируем первую транзакцию
```sql
-- В первой сессии
COMMIT;
```
![создание таблицы shipments](HW-01/image/session1_cpmmit.png)

##### 4.6 Повторно читаем во второй сессии
```sql
select * from shipments;
```
![создание таблицы shipments](HW-01/image/session2_read.png)

###### Результат:
Теперь запись о сахаре видна.
######
После COMMIT изменения стали постоянными. Когда во второй сессии выполнился SELECT, он создал новый "снимок" данных, который включил все закоммиченные изменения, включая поставку сахара в Азию.

##### 4.6 Вывод для READ COMMITTED
Уровень по умолчанию хорошо защищает от грязного чтения, но допускает неповторяемое чтение: если в рамках одной транзакции выполнить SELECT дважды, можно получить разные результаты, если между этими запросами другая транзакция успеет закоммитить изменения.

