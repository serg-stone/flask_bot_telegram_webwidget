import os
import json
import logging
from dotenv import load_dotenv
import pytz
from datetime import datetime

# Google Sheets
from google.oauth2 import service_account
from googleapiclient.discovery import build

# OpenAI Assistant
import openai

# Telegram
from telegram import Bot, Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ConversationHandler

# Загрузка переменных окружения
load_dotenv()

# === Константы для FSM ===
(STATE_NAME, STATE_PHONE, STATE_SERVICE, STATE_DATE, STATE_DOCUMENTS, STATE_COMMENT) = range(6)

# === Переменные окружения ===
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_GROUP_ID = os.getenv('TELEGRAM_GROUP_ID')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_ASSISTANT_ID = os.getenv('OPENAI_ASSISTANT_ID')
GOOGLE_SHEET_ID = os.getenv('GOOGLE_SHEET_ID')
NGROK_URL = os.getenv('NGROK_URL')

# === Логирование ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# === Telegram Bot будет создан в main.py ===
# bot = Bot(token=TELEGRAM_BOT_TOKEN)  # Убираем дублирование

# === Инициализация OpenAI ===
openai.api_key = OPENAI_API_KEY

# === Инициализация Google Sheets ===
GOOGLE_SERVICE_ACCOUNT_FILE = 'assistent-jura-2cef395ce813.json'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

try:
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    sheets_service = build('sheets', 'v4', credentials=credentials)
    sheet = sheets_service.spreadsheets()
    logger.info("Google Sheets API инициализирован")
except Exception as e:
    logger.error(f"Ошибка инициализации Google Sheets: {e}")
    sheet = None

# === Клавиатуры ===
main_keyboard = ReplyKeyboardMarkup([
    ["Быстрая запись", "Консультация"]
], resize_keyboard=True)

# Список услуг
services = [
    "Правовая консультация",
    "Выезд адвоката к клиенту", 
    "Письменное правовое заключение",
    "Составление документов",
    "Участие в переговорах",
    "Представительство перед организациями",
    "Представительство в судах",
    "Защита по уголовным/административным делам",
    "Интересы потерпевших (дознание, следствие, суд)"
]

service_keyboard = ReplyKeyboardMarkup(
    [[service] for service in services], 
    resize_keyboard=True, 
    one_time_keyboard=True
)

# === Хранилище данных пользователей ===
user_data = {}  # user_id -> данные заявки
user_threads = {}  # user_id -> thread_id для OpenAI

# === Функция: сохранение заявки в Google Sheets ===
def save_application_to_sheets(data: dict):
    """Сохраняет заявку в Google Таблицу"""
    if not sheet:
        logger.error("Google Sheets не инициализирован")
        return None
        
    try:
        # Обрабатываем данные перед записью
        documents = data.get('documents', '').strip()
        if not documents or documents.lower() in ['нет', 'no', '']:
            documents = 'нет'
            
        comment = data.get('comment', '').strip()  
        if not comment or comment.lower() in ['нет', 'no', '']:
            comment = 'нет'
            
        source = data.get('source', '')
        if source == 'Телеграм':
            source = 'Телеграм'
        elif source == 'website':
            source = 'Виджет'
        elif source == 'Виджет':
            source = 'Виджет'
        
        values = [[
            data.get('name', ''),
            data.get('phone', ''),
            data.get('service', ''),
            data.get('date', ''),
            documents,
            comment,
            source,
            datetime.now(pytz.timezone('Europe/Moscow')).strftime('%Y-%m-%d %H:%M:%S')
        ]]
        
        result = sheet.values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range='A1',
            valueInputOption='USER_ENTERED',
            body={'values': values}
        ).execute()
        
        logger.info(f"Заявка сохранена в Google Sheets: {data.get('name', 'Без имени')}")
        return result
        
    except Exception as e:
        logger.error(f"Ошибка сохранения в Google Sheets: {e}")
        return None

# === Функция: отправка уведомления в Telegram группу ===
def send_telegram_notification(text: str, bot=None):
    """Отправляет уведомление в служебный Telegram чат"""
    try:
        if bot:
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(bot.initialize())
                loop.run_until_complete(bot.send_message(chat_id=TELEGRAM_GROUP_ID, text=text))
                loop.run_until_complete(bot.shutdown())
                logger.info("Уведомление отправлено в Telegram группу")
            finally:
                loop.close()
        else:
            logger.warning("Bot не передан для отправки уведомления")
    except Exception as e:
        logger.error(f"Ошибка отправки в Telegram: {e}")

# === Функция: обработка OpenAI function calls ===
def handle_function_call(function_name: str, arguments: dict, source: str = 'Виджет'):
    """Обрабатывает вызовы функций от OpenAI Assistant"""
    try:
        if function_name == "save_booking_data":
            # Проверяем обязательные поля
            required_fields = {
                'name': 'имя клиента',
                'phone': 'номер телефона', 
                'service': 'название услуги',
                'datetime': 'дату и время встречи'
            }
            
            missing_fields = []
            for field, description in required_fields.items():
                value = arguments.get(field, '').strip()
                if not value:
                    missing_fields.append(description)
            
            # Если есть пустые обязательные поля, запрашиваем их
            if missing_fields:
                missing_text = ', '.join(missing_fields)
                return {
                    "success": False,
                    "message": f"Для оформления заявки необходимо уточнить: {missing_text}. Пожалуйста, запросите эти данные у клиента."
                }
            
            # Все обязательные поля заполнены - сохраняем заявку
            booking_data = {
                'name': arguments.get('name', '').strip(),
                'phone': arguments.get('phone', '').strip(),
                'service': arguments.get('service', '').strip(),
                'date': arguments.get('datetime', '').strip(),
                'documents': arguments.get('documents', '').strip() or 'нет',
                'comment': arguments.get('comments', '').strip() or 'нет',
                'source': source,  # Используем переданный источник
            }
            
            # Сохраняем в Google Sheets
            result = save_application_to_sheets(booking_data)
            
            if result:
                logger.info(f"Заявка сохранена через OpenAI function: {booking_data.get('name', 'Без имени')}")
                return {
                    "success": True,
                    "message": f"✅ Заявка успешно сохранена в системе!\n\nДанные клиента:\n👤 Имя: {booking_data['name']}\n📞 Телефон: {booking_data['phone']}\n⚖️ Услуга: {booking_data['service']}\n📅 Дата: {booking_data['date']}\n\nМы свяжемся с клиентом для уточнения деталей."
                }
            else:
                return {
                    "success": False,
                    "message": "Произошла ошибка при сохранении заявки в системе. Попробуйте еще раз или обратитесь к администратору."
                }
        else:
            logger.warning(f"Неизвестная функция: {function_name}")
            return {
                "success": False,
                "message": f"Функция {function_name} не поддерживается"
            }
            
    except Exception as e:
        logger.error(f"Ошибка выполнения функции {function_name}: {e}")
        return {
            "success": False,
            "message": "Произошла техническая ошибка при обработке заявки"
        }

# === Функция: работа с OpenAI Assistant ===
def get_assistant_response(message: str, thread_id: str = None, source: str = 'Виджет'):
    """Получает ответ от OpenAI Assistant с поддержкой function calls"""
    try:
        # Создаём новый thread если не передан
        if not thread_id:
            thread = openai.beta.threads.create()
            thread_id = thread.id
            
        # Добавляем сообщение в thread
        openai.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=message
        )
        
        # Запускаем assistant
        run = openai.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=OPENAI_ASSISTANT_ID
        )
        
        # Ожидаем завершения с обработкой function calls
        import time
        while True:
            run_status = openai.beta.threads.runs.retrieve(
                thread_id=thread_id, 
                run_id=run.id
            )
            
            # Обрабатываем требуемые действия (function calls)
            if run_status.status == "requires_action":
                logger.info(f"🔧 OpenAI требует выполнения функций")
                tool_calls = run_status.required_action.submit_tool_outputs.tool_calls
                logger.info(f"🔧 Количество функций для вызова: {len(tool_calls)}")
                tool_outputs = []
                
                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    arguments = json.loads(tool_call.function.arguments)
                    
                    logger.info(f"OpenAI вызывает функцию: {function_name} с аргументами: {arguments}")
                    
                    # Выполняем функцию с переданным источником
                    result = handle_function_call(function_name, arguments, source)
                    
                    tool_outputs.append({
                        "tool_call_id": tool_call.id,
                        "output": json.dumps(result)
                    })
                
                # Отправляем результаты функций обратно в OpenAI
                openai.beta.threads.runs.submit_tool_outputs(
                    thread_id=thread_id,
                    run_id=run.id,
                    tool_outputs=tool_outputs
                )
                
            elif run_status.status in ["completed", "failed", "cancelled"]:
                logger.info(f"🏁 OpenAI завершен со статусом: {run_status.status}")
                break
                
            time.sleep(1)
            
        # Получаем ответ
        if run_status.status == "completed":
            messages = openai.beta.threads.messages.list(thread_id=thread_id)
            logger.info(f"Получено {len(messages.data)} сообщений в thread")
            
            # Берём ПЕРВОЕ сообщение (самое новое) от assistant
            for i, msg in enumerate(messages.data):
                logger.info(f"Сообщение {i}: роль={msg.role}, создано={msg.created_at}")
                if msg.role == "assistant":
                    response_text = msg.content[0].text.value
                    logger.info(f"Возвращаем ответ assistant: '{response_text[:100]}...'")
                    
                    # Проверяем, если Assistant говорит о сохранении записи
                    if "сохраню вашу запись" in response_text.lower() or "все данные собраны" in response_text.lower():
                        logger.info("🔄 Обнаружено намерение сохранить запись, извлекаем данные из thread")
                        try:
                            # Извлекаем данные из всех сообщений thread
                            booking_data = extract_booking_data_from_thread(messages.data)
                            if booking_data:
                                logger.info(f"📝 Извлеченные данные записи: {booking_data}")
                                success = save_application_to_sheets(booking_data)
                                if success:
                                    logger.info("✅ Запись успешно сохранена в Google Sheets из веб-виджета")
                                else:
                                    logger.error("❌ Ошибка сохранения в Google Sheets")
                        except Exception as e:
                            logger.error(f"❌ Ошибка при извлечении данных записи: {e}")
                    
                    return response_text, thread_id
                    
        logger.error(f"OpenAI Assistant завершился со статусом: {run_status.status}")
        return "Извините, произошла ошибка при получении ответа.", thread_id
        
    except Exception as e:
        logger.error(f"Ошибка OpenAI Assistant: {e}")
        return "Извините, сервис временно недоступен.", thread_id

def extract_booking_data_from_thread(messages):
    """Извлекает данные записи из сообщений thread"""
    try:
        # Собираем все сообщения пользователя
        user_messages = []
        for msg in reversed(messages):  # От старых к новым
            if msg.role == "user":
                user_messages.append(msg.content[0].text.value)
        
        logger.info(f"🔍 Сообщения пользователя: {user_messages}")
        
        # Пытаемся извлечь данные (простая логика)
        booking_data = {
            'name': '',
            'phone': '',
            'service': '',
            'date': '',
            'documents': 'нет',
            'comment': 'нет',
            'source': 'Виджет'
        }
        
        # Умный поиск данных в сообщениях
        for i, msg in enumerate(user_messages):
            msg_lower = msg.lower().strip()
            
            # Ищем имя (не содержит цифр, не ключевые слова)
            if not booking_data['name'] and not any(char.isdigit() for char in msg) and len(msg.split()) <= 3:
                if msg_lower not in ['да', 'нет', 'да, хочу записаться', 'хочу записаться', 'записаться']:
                    booking_data['name'] = msg.strip()
                    logger.info(f"📝 Найдено имя: {booking_data['name']}")
            
            # Ищем телефон (содержит цифры и длинный)
            if not booking_data['phone'] and any(char.isdigit() for char in msg) and len(msg) >= 7:
                # Простая проверка на телефон
                digits = ''.join(filter(str.isdigit, msg))
                if len(digits) >= 7:
                    booking_data['phone'] = msg.strip()
                    logger.info(f"📱 Найден телефон: {booking_data['phone']}")
            
            # Ищем дату (содержит цифры и возможные форматы даты)
            if not booking_data['date'] and any(char.isdigit() for char in msg):
                if any(pattern in msg_lower for pattern in ['.', '/', 'ноябр', 'декабр', 'январ', 'февр', 'март', 'апрел', 'май', 'июн', 'июл', 'август', 'сентябр', 'октябр']):
                    booking_data['date'] = msg.strip()
                    logger.info(f"📅 Найдена дата: {booking_data['date']}")
            
            # Ищем услугу (длинное описание, не имя, не телефон, не дата)
            if not booking_data['service'] and len(msg) > 10:
                if not any(char.isdigit() for char in msg) or 'консультация' in msg_lower or 'адвокат' in msg_lower or 'суд' in msg_lower:
                    if msg_lower not in ['да, хочу записаться', 'хочу записаться']:
                        booking_data['service'] = msg.strip()
                        logger.info(f"⚖️ Найдена услуга: {booking_data['service']}")
        
        # Оставшиеся сообщения как документы и комментарии
        for i, msg in enumerate(user_messages):
            msg_lower = msg.lower().strip()
            # Если сообщение не является именем, телефоном, услугой или датой
            if (msg != booking_data['name'] and msg != booking_data['phone'] and 
                msg != booking_data['service'] and msg != booking_data['date']):
                
                # Проверяем на документы
                if ('паспорт' in msg_lower or 'документ' in msg_lower or 'справк' in msg_lower or 
                    'свидетельство' in msg_lower or 'удостовер' in msg_lower):
                    booking_data['documents'] = msg.strip()
                    logger.info(f"📄 Найдены документы: {booking_data['documents']}")
                
                # Остальное как комментарии
                elif len(msg) > 3 and msg_lower not in ['да', 'нет', 'да, хочу записаться', 'хочу записаться']:
                    if booking_data['comment'] == 'нет':
                        booking_data['comment'] = msg.strip()
                    else:
                        booking_data['comment'] += f"; {msg.strip()}"
                    logger.info(f"💬 Найден комментарий: {msg.strip()}")
        
        # Проверяем, что есть основные данные
        if booking_data['name'] and booking_data['phone']:
            logger.info(f"✅ Успешно извлечены данные записи")
            return booking_data
        else:
            logger.warning(f"⚠️ Недостаточно данных для записи: имя={booking_data['name']}, телефон={booking_data['phone']}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Ошибка извлечения данных: {e}")
        return None

# === Telegram handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    await update.message.reply_text(
        "Здравствуйте! Я бот-ассистент сервиса 'Твоё право'.\n\n"
        "Выберите нужное действие:",
        reply_markup=main_keyboard
    )

async def handle_mode_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик выбора режима работы"""
    text = update.message.text
    user_id = update.effective_user.id
    
    if text == "Быстрая запись":
        user_data[user_id] = {}
        await update.message.reply_text(
            "Давайте оформим заявку.\nВведите ваше имя:",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.info(f"Быстрая запись: переход в STATE_NAME для user_id={user_id}")
        return STATE_NAME
        
    elif text == "Консультация":
        await update.message.reply_text(
            "Вы в режиме консультации. Задайте ваш вопрос:",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.info(f"Консультация: пользователь {user_id} перешёл в режим консультации")
        return ConversationHandler.END
        
    else:
        await update.message.reply_text(
            "Пожалуйста, выберите действие с помощью кнопок:",
            reply_markup=main_keyboard
        )

# === FSM handlers для быстрой записи ===
async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение имени клиента"""
    logger.info(f"🎯 get_name вызвана! user_id={update.effective_user.id}, text='{update.message.text}'")
    logger.info(f"Текущее состояние conversation: {context.user_data}")
    
    user_id = update.effective_user.id
    user_data[user_id]['name'] = update.message.text
    
    await update.message.reply_text("Введите ваш номер телефона:")
    logger.info("✅ Отправлен запрос телефона, переход в STATE_PHONE")
    return STATE_PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение телефона клиента"""
    user_id = update.effective_user.id
    user_data[user_id]['phone'] = update.message.text
    
    await update.message.reply_text(
        "Выберите услугу:",
        reply_markup=service_keyboard
    )
    return STATE_SERVICE

async def get_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение выбранной услуги"""
    user_id = update.effective_user.id
    selected_service = update.message.text
    
    if selected_service not in services:
        await update.message.reply_text(
            "Пожалуйста, выберите услугу из списка:",
            reply_markup=service_keyboard
        )
        return STATE_SERVICE
        
    user_data[user_id]['service'] = selected_service
    await update.message.reply_text(
        "Укажите желаемые дату и время (например: 25.12.2024 15:00):",
        reply_markup=ReplyKeyboardRemove()
    )
    return STATE_DATE

async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение даты и времени"""
    user_id = update.effective_user.id
    user_data[user_id]['date'] = update.message.text
    
    await update.message.reply_text(
        "Перечислите документы, которые есть на руках (или напишите 'нет'):"
    )
    return STATE_DOCUMENTS

async def get_documents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение информации о документах"""
    user_id = update.effective_user.id
    user_data[user_id]['documents'] = update.message.text
    
    await update.message.reply_text(
        "Добавьте комментарий или дополнительную информацию (или напишите 'нет'):"
    )
    return STATE_COMMENT

async def get_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение сбора данных и сохранение заявки"""
    user_id = update.effective_user.id
    user_data[user_id]['comment'] = update.message.text
    user_data[user_id]['source'] = 'Телеграм'  # Явно устанавливаем источник для FSM
    
    # Сохраняем заявку
    application_data = user_data[user_id]
    save_result = save_application_to_sheets(application_data)
    
    # Отправляем уведомление в группу
    # Подготавливаем данные для уведомления (аналогично save_application_to_sheets)
    documents = application_data.get('documents', '').strip()
    if not documents or documents.lower() in ['нет', 'no', '']:
        documents = 'нет'
        
    comment = application_data.get('comment', '').strip()  
    if not comment or comment.lower() in ['нет', 'no', '']:
        comment = 'нет'
    
    notification_text = (
        f"📋 Новая заявка из Telegram:\n\n"
        f"👤 Имя: {application_data.get('name', '')}\n"
        f"📞 Телефон: {application_data.get('phone', '')}\n"
        f"⚖️ Услуга: {application_data.get('service', '')}\n"
        f"📅 Дата: {application_data.get('date', '')}\n"
        f"📄 Документы: {documents}\n"
        f"💬 Комментарий: {comment}"
    )
    # send_telegram_notification будет вызвана из main.py с правильным bot
    
    await update.message.reply_text(
        "✅ Спасибо! Ваша заявка принята.\n"
        "Мы свяжемся с вами в ближайшее время.",
        reply_markup=main_keyboard
    )
    
    # Очищаем данные пользователя
    user_data.pop(user_id, None)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущей операции"""
    user_id = update.effective_user.id
    user_data.pop(user_id, None)
    
    await update.message.reply_text(
        "Операция отменена.",
        reply_markup=main_keyboard
    )
    return ConversationHandler.END

# === Обработчик всех сообщений для отладки ===
async def debug_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отладочный обработчик - показывает все входящие сообщения"""
    user_id = update.effective_user.id
    message = update.message.text if update.message else "Нет текста"
    logger.info(f"🔍 DEBUG: получено сообщение от user_id={user_id}, text='{message}'")
    logger.info(f"🔍 DEBUG: состояние context.user_data={context.user_data}")

# === Обработчик консультаций ===
async def consultation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик консультаций через OpenAI Assistant"""
    user_id = update.effective_user.id
    message = update.message.text
    
    logger.info(f"💬 Консультация от user_id={user_id}, message='{message}'")
    
    try:
        # Получаем thread_id для пользователя или создаём новый
        thread_id = user_threads.get(user_id)
        logger.info(f"Thread ID для пользователя {user_id}: {thread_id}")
        
        # Отправляем сообщение "печатает"
        await update.message.reply_text("⏳ Обрабатываю ваш вопрос...")
        
        # Получаем ответ от OpenAI Assistant
        logger.info("Отправляю запрос к OpenAI Assistant...")
        answer, new_thread_id = get_assistant_response(message, thread_id, 'Телеграм')
        user_threads[user_id] = new_thread_id
        
        logger.info(f"Получен ответ от OpenAI: длина {len(answer)} символов")
        logger.info(f"Новый thread_id: {new_thread_id}")
        
        await update.message.reply_text(answer)
        logger.info("Ответ отправлен пользователю")
        
    except Exception as e:
        logger.error(f"Ошибка в consultation_handler: {e}", exc_info=True)
        await update.message.reply_text(
            "Извините, произошла ошибка при обработке вашего вопроса. Попробуйте ещё раз."
        )

# === Экспорт для main.py ===
__all__ = [
    'start', 'handle_mode_choice', 'get_name', 'get_phone', 'get_service',
    'get_date', 'get_documents', 'get_comment', 'cancel', 'consultation_handler',
    'STATE_NAME', 'STATE_PHONE', 'STATE_SERVICE', 'STATE_DATE', 'STATE_DOCUMENTS', 'STATE_COMMENT',
    'bot', 'logger', 'NGROK_URL', 'save_application_to_sheets', 'get_assistant_response'
]
