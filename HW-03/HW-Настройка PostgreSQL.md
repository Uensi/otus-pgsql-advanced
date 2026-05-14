# Домашнее задание N3: Настройка PostgteSQL

## Информация о проекте
- **Название ВМ:** bananaflow-30081986
- **Дата выполнения:** 2026-05-014
- **Версия PostgreSQL:** 18

## 1. Установка PostgreSQL 18 и первичная настройка 
```bash
sudo -i
dnf update -y
# Устанавливаем PostgreSQL 18
dnf install postgresql18 postgresql18-contrib
# Инициализируем кластер
postgresql-18-setup initdb
# Запускаем и добавляем в автозагрузку
systemctl enable postgresql-18.service --now
systemctl status postgresql-18.service
```
![Установка postgresql](HW-03/image/install_postgresql.png)

## 2. Создаем таблицы с данными о перевозках

#### 2.1 Подключаемся к БД
```bash
sudo -u postgres psql
```
```sql
-- Создаём базу данных
CREATE DATABASE transport;

-- Подключаемся к ней
\c transport;

-- Создаём таблицу перевозок
CREATE TABLE shipments (
    id SERIAL PRIMARY KEY,
    cargo_type VARCHAR(50) NOT NULL,
    weight_kg NUMERIC(10,2),
    destination VARCHAR(100),
    created_at TIMESTAMP DEFAULT NOW()
);

-- Добавляем несколько записей
INSERT INTO shipments (cargo_type, weight_kg, destination) VALUES
('Electronics', 120.5, 'Moscow'),
('Furniture', 350.0, 'Saint Petersburg'),
('Food', 45.2, 'Kazan');

-- Проверяем
SELECT * FROM shipments;
```

![Создание таблиц](HW-03/image/insert_table.png)

## 3. Добавление внешнего диска

#### 3.1 Выполняем SQL скрипт через временный контейнер
```bash
docker run --rm -it \
  --network host \
  -e PGPASSWORD=secret \
  postgres:17 \
  psql -h localhost -U postgres
```
######

флаг --network host: Этот флаг заставляет контейнер-клиент использовать сеть сервера напрямую. Это самый простой способ для временного клиента обратиться к серверу через localhost

![psql_client](HW-02/image/psql_client.png)

```sql
create table shipments(id serial, product_name text, quantity int, destination text);

insert into shipments(product_name, quantity, destination) values('bananas', 1000, 'Europe');
insert into shipments(product_name, quantity, destination) values('bananas', 1500, 'Asia');
insert into shipments(product_name, quantity, destination) values('bananas', 2000, 'Africa');
insert into shipments(product_name, quantity, destination) values('coffee', 500, 'USA');
insert into shipments(product_name, quantity, destination) values('coffee', 700, 'Canada');
insert into shipments(product_name, quantity, destination) values('coffee', 300, 'Japan');
insert into shipments(product_name, quantity, destination) values('sugar', 1000, 'Europe');
insert into shipments(product_name, quantity, destination) values('sugar', 800, 'Asia');
insert into shipments(product_name, quantity, destination) values('sugar', 600, 'Africa');
insert into shipments(product_name, quantity, destination) values('sugar', 400, 'USA');"
```

![insert](HW-02/image/insert.png)

## 4. Подключение с ноутбука

#### 4.1 Изменяем конфигурацию в контейнере
Нужно попросить PostgreSQL слушать не только localhost, а все интерфейсы. Для этого нужно добавить параметр -c listen_addresses='*' при запуске

#### 4.2 даляем старый контейнер и создаем новый данные должны сохраниться
```bash
# Останавливаем и удаляем старый контейнер

docker stop pg-server
docker rm pg-server

# Запускаем новый контейнер с разрешением слушать все интерфейсы
docker run --name pg-server \
  -e POSTGRES_PASSWORD=secret \
  -p 0.0.0.0:5432:5432 \
  -v /var/lib/postgres:/var/lib/postgresql/data \
  -d postgres:17 \
  -c listen_addresses='*'
```
![docker_run2](HW-02/image/docker_run2.png)

#### 4.3 Подключаемся с ноутбука

![pgadmin](HW-02/image/pgadmin.png)

## 5. Проверка сохранности данных

#### 5.1 Останавливаем и удаляем контейнер-сервер
```bash
docker stop pg-server
docker rm pg-server
```

#### 5.2 Создаем контейнер заново
```bash
docker run --name pg-server \
  -e POSTGRES_PASSWORD=secret \
  -p 0.0.0.0:5432:5432 \
  -v /var/lib/postgres:/var/lib/postgresql/data \
  -d postgres:17 \
  -c listen_addresses='*'
```

![docker_run2](HW-02/image/docker_run3.png)

#### 5.3 Проверяем данные
```bash
docker run --rm -it --network host -e PGPASSWORD=secret postgres:17 psql -h localhost -U postgres -c "SELECT * FROM shipments;"
```

![psql_client](HW-02/image/psql_client2.png)

### Возможные проблемы и их решения

- **`permission denied` при запуске Docker**  
  → Добавьте пользователя в группу `docker`: `sudo usermod -aG docker $USER`

- **`initdb: error: directory exists but is not empty`**  
  → Очистите каталог или используйте `PGDATA` для указания подкаталога

- **Данные не сохраняются после удаления контейнера**  
  → Проверьте путь монтирования — он должен быть `/var/lib/postgresql/data`, а не `/var/lib/postgresql`

- **Не могу подключиться с ноутбука**  
  → Убедитесь, что `listen_addresses='*'` и порт `5432` открыт в firewall

- **Контейнер не запускается из-за прав**  
  → Установите правильные права на каталог: `chown -R 999:999 /var/lib/postgres`