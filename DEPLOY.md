# Розгортання на сервері

## Перший раз (initial setup)

```bash
# 1. Клонувати репозиторій
cd ~
git clone https://github.com/lamerroma/localai-translator.git translater
cd translater

# 2. Створити віртуальне оточення
sudo apt install python3-venv -y
python3 -m venv venv
source venv/bin/activate

# 3. Встановити залежності
pip install -r requirements.txt

# 4. Перевірити що запускається вручну
python translate_server.py
# Має бути: Uvicorn running on http://0.0.0.0:7860
# Перевірити в браузері: http://192.168.88.59:7860
# Ctrl+C щоб зупинити

# 5. Встановити systemd service
sudo cp translator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable translator
sudo systemctl start translator

# 6. Перевірити статус
sudo systemctl status translator
```

## Оновлення (після змін в git)

```bash
cd ~/translater
git pull
source venv/bin/activate
pip install -r requirements.txt  # якщо змінились залежності
sudo systemctl restart translator
sudo systemctl status translator
```

## Корисні команди

| Дія | Команда |
|-----|---------|
| Статус | `sudo systemctl status translator` |
| Перезапуск | `sudo systemctl restart translator` |
| Зупинити | `sudo systemctl stop translator` |
| Логи в реальному часі | `sudo journalctl -u translator -f` |
| Останні 50 логів | `sudo journalctl -u translator -n 50` |
| Вимкнути автозапуск | `sudo systemctl disable translator` |
