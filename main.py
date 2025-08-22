from flask import Flask, request, jsonify
from flask_cors import CORS
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler
import json
import logging
import asyncio
import threading
from functions import (
    start, handle_mode_choice, get_name, get_phone, get_service,
    get_date, get_documents, get_comment, cancel, consultation_handler, debug_handler,
    STATE_NAME, STATE_PHONE, STATE_SERVICE, STATE_DATE, STATE_DOCUMENTS, STATE_COMMENT,
    logger, NGROK_URL, save_application_to_sheets, get_assistant_response,
    TELEGRAM_BOT_TOKEN
)

# Создаём Flask приложение
app = Flask(__name__)
CORS(app)  # Разрешаем CORS для веб-виджета

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# Создаём глобальный Telegram Application
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

# Глобальная инициализация Application
import asyncio
import threading

# Глобальный event loop для Telegram
telegram_loop = None
telegram_thread = None

def run_telegram_loop():
    """Запускает постоянный event loop для Telegram в отдельном потоке"""
    global telegram_loop
    telegram_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(telegram_loop)
    
    async def init_and_run():
        await application.initialize()
        await application.bot.initialize()
        logger.info("Application и Bot инициализированы в постоянном loop")
        # Держим loop открытым
        while True:
            await asyncio.sleep(1)
    
    try:
        telegram_loop.run_until_complete(init_and_run())
    except Exception as e:
        logger.error(f"Ошибка в telegram loop: {e}")

def init_application():
    """Запускаем Telegram в отдельном потоке с постоянным loop"""
    global telegram_thread
    telegram_thread = threading.Thread(target=run_telegram_loop, daemon=True)
    telegram_thread.start()
    
    # Ждём инициализации
    import time
    time.sleep(2)

# === Настройка ConversationHandler ===
conversation_handler = ConversationHandler(
    entry_points=[
        CommandHandler('start', start),
        MessageHandler(filters.Regex('^(Быстрая запись|Консультация)$'), handle_mode_choice)
    ],
    states={
        STATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
        STATE_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
        STATE_SERVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_service)],
        STATE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
        STATE_DOCUMENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_documents)],
        STATE_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_comment)],
    },
    fallbacks=[CommandHandler('cancel', cancel)],
    allow_reentry=True
)

# Добавляем handlers в Application
application.add_handler(conversation_handler)
# Consultation handler должен быть ПЕРЕД debug handler
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, consultation_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, debug_handler))

# === Flask Routes ===

@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint для Telegram"""
    try:
        logger.info('Telegram webhook получил запрос')
        
        # Получаем данные от Telegram
        data = request.get_json()
        if not data:
            logger.error('Нет данных в webhook запросе')
            return 'No data', 400
            
        logger.info(f'Webhook данные: {data}')
        
        # Создаём Update объект
        update = telegram.Update.de_json(data, application.bot)
        
        # Обрабатываем update синхронно в telegram loop
        try:
            # Используем глобальный telegram_loop без дополнительных потоков
            future = asyncio.run_coroutine_threadsafe(
                application.process_update(update), 
                telegram_loop
            )
            future.result(timeout=10)  # Ждём результат максимум 10 сек
        except Exception as e:
            logger.error(f"Ошибка обработки update: {e}")
        
        logger.info('Update обработан успешно')
        return 'OK', 200
        
    except Exception as e:
        logger.error(f'Ошибка в webhook: {e}', exc_info=True)
        return 'Error', 500

@app.route('/api/chat', methods=['POST'])
def chat_api():
    """API для веб-виджета: консультации через OpenAI Assistant"""
    try:
        data = request.get_json()
        message = data.get('message', '')
        thread_id = data.get('thread_id')
        
        if not message:
            return jsonify({'error': 'Сообщение не может быть пустым'}), 400
            
        # Получаем ответ от OpenAI Assistant
        answer, new_thread_id = get_assistant_response(message, thread_id, 'Виджет')
        
        return jsonify({
            'response': answer,
            'thread_id': new_thread_id
        })
        
    except Exception as e:
        logger.error(f'Ошибка API чата: {e}')
        return jsonify({'error': 'Внутренняя ошибка сервера'}), 500

@app.route('/api/booking', methods=['POST'])
def booking_api():
    """API для веб-виджета: быстрая запись к адвокату"""
    try:
        data = request.get_json()
        
        # Валидация обязательных полей
        required_fields = ['name', 'phone', 'service', 'date']
        for field in required_fields:
            if not data.get(field):
                return jsonify({'error': f'Поле {field} обязательно для заполнения'}), 400
        
        # Подготавливаем данные для сохранения
        booking_data = {
            'name': data.get('name'),
            'phone': data.get('phone'),
            'service': data.get('service'),
            'date': data.get('date'),
            'documents': data.get('documents', ''),
            'comment': data.get('comment', ''),
            'source': 'Виджет'
        }
        
        # Сохраняем в Google Sheets
        result = save_application_to_sheets(booking_data)
        
        if result:
            # Отправляем уведомление в Telegram
            from functions import send_telegram_notification
            notification_text = (
                f"📋 Новая заявка с сайта:\n\n"
                f"👤 Имя: {booking_data['name']}\n"
                f"📞 Телефон: {booking_data['phone']}\n"
                f"⚖️ Услуга: {booking_data['service']}\n"
                f"📅 Дата: {booking_data['date']}\n"
                f"📄 Документы: {booking_data['documents']}\n"
                f"💬 Комментарий: {booking_data['comment']}"
            )
            send_telegram_notification(notification_text, application.bot)
            
            return jsonify({'success': True, 'message': 'Заявка успешно отправлена'})
        else:
            return jsonify({'error': 'Ошибка при сохранении заявки'}), 500
            
    except Exception as e:
        logger.error(f'Ошибка API записи: {e}')
        return jsonify({'error': 'Внутренняя ошибка сервера'}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Проверка работоспособности сервиса"""
    return jsonify({
        'status': 'OK',
        'service': 'Твоё право - Адвокатские услуги',
        'telegram_bot': 'активен',
        'flask_api': 'активен'
    })

@app.route('/api/services', methods=['GET'])
def get_services():
    """Получение списка услуг для веб-виджета"""
    from functions import services
    return jsonify({'services': services})

@app.route('/', methods=['GET'])
def index():
    """Главная страница"""
    return jsonify({
        'message': 'Сервис "Твоё право" - Адвокатские услуги',
        'telegram_bot': '@assist_jura_bot',
        'api_endpoints': {
            'chat': '/api/chat',
            'booking': '/api/booking', 
            'services': '/api/services',
            'health': '/health'
        }
    })

# === Запуск приложения ===
if __name__ == '__main__':
    logger.info("🚀 Запуск системы 'Твоё право'...")
    
    # Инициализируем Application глобально
    init_application()
    
    # Устанавливаем webhook для Telegram
    try:
        webhook_url = f"{NGROK_URL}/webhook"
        
        # Используем синхронный requests для установки webhook
        import requests
        telegram_api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
        response = requests.post(telegram_api_url, data={'url': webhook_url})
        
        if response.status_code == 200:
            logger.info(f"✅ Telegram webhook установлен: {webhook_url}")
        else:
            logger.error(f"❌ Ошибка установки webhook: {response.text}")
    except Exception as e:
        logger.error(f"❌ Ошибка установки webhook: {e}")
    
    # Запускаем Flask сервер
    logger.info("🌐 Запуск Flask API сервера на порту 5000...")
    logger.info("📱 Telegram бот готов к работе")
    logger.info("🌍 Веб-API готов для виджета")
    
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=False
    )
