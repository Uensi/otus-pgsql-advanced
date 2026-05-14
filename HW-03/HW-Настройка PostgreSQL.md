# Домашнее задание N3: Настройка PostgteSQL

## Информация о проекте
- **Название ВМ:** bananaflow-30081986
- **Дата выполнения:** 2026-05-14
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

#### 3.1 Создаем, разде куда будем монтировать диск
```bash
mkdir -p /mnt/pg_data/
```
#### 3.2 Смотрим все диски и создаем раздел
```bash
lsblk
```
![Диск](HW-03/image/disk.png)

```bash
parted -s /dev/sdb mklabel gpt mkpart primary 0% 100% set 1 lvm on
pvcreate /dev/sdb1 && vgcreate vg_db /dev/sdb1 && lvcreate -l 100%FREE -n lv_db vg_db
mkfs.ext4 /dev/vg_db/lv_db
echo "UUID=$(sudo blkid -s UUID -o value /dev/mapper/vg_db-lv_db) /mnt/pg_data ext4 defaults 1 2" | sudo tee -a /etc/fstab
systemctl daemon-reload
mount /var/lib/pgsql
```
![LVM](HW-03/image/lvm.png)

#### 3.3 Даем права на смонтированный каталог пользователю postgres
```bash
chown -R postgres: /mnt/pg_data/
chmod 700 /mnt/pg_data
```
## 4. Перенос базы данных на внешний диск

#### 4.1 Останавливаем службу postgresql
```bash
systemctl stop postgresql-18.service
```

#### 4.2 Создаём директорию на новом диске
```bash
mkdir -p /mnt/pg_data/data
```
#### 4.3 Копируем БД на внешний диск
```bash
cp -rp /var/lib/pgsql/18/data/* /mnt/pg_data/data
```
![Новая БД](HW-03/image/new_basa.png)

#### 4.4 Переименовываем старую БД
```bash
mv /var/lib/pgsql/18/data /var/lib/pgsql/18/data.backup
```
![Бэкап](HW-03/image/renamedbd.png)

#### 4.5 Самый простой способ, создаем символическую ссылку на новый каталог с БД и даем права на каталог
```bash
ln -s /mnt/pg_data/data/ /var/lib/pgsql/18/
chown postgres: /var/lib/pgsql/18/data
chmod 700 /var/lib/pgsql/18/data
```
!ссылка](HW-03/image/ln.png)

## 5. Запуск и проверка
```bash
systemctl start postgresql-18
df -h /var/lib/pgsql/18/data
```
![ext](HW-03/image/externalBD.png)

## 6. Проверка данных через консоль
```bash
sudo -u postgres psql -d transport -c 'SELECT * FROM shipments;'
```
![Проверка](HW-03/image/worked.png)


## 6. Перенос БД через конфиграцию в файле postgresql.conf (data_directory)

#### 6.1 Останавливаем службу postgresql
```bash
systemctl stop postgresql-18.service
```
#### 6.1 Возвращаем каталог с бэкапов БД назад и удаляем символьную ссылку
```bash
rm -rf /var/lib/pgsql/18/data
mv /var/lib/pgsql/18/data.backup /var/lib/pgsql/18/data
```
![Другой способ](HW-03/image/sposob2.png)


#### 6.2 Прописываем в конфиг файле новый каталог с БД на внешний диск
```bash
nano /var/lib/pgsql/18/data/postgresql.conf
```
![Конфиг](HW-03/image/config.png)

## 7. Запуск и проверка
```bash
systemctl start postgresql-18
systemctl status postgresql-18
sudo -u postgres psql -d postgres -c "SHOW data_directory;"
```
![Проверка](HW-03/image/directory.png)

## 8. Проверка данных через консоль
```bash
sudo -u postgres psql -d transport -c 'SELECT * FROM shipments;'
```
![Проверка](HW-03/image/worked1.png)