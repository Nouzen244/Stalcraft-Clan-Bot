"""
STALCRAFT Clan Bot v3.0 - Полная переработка
Трекинг ВСЕХ голосовых каналов, КВ, Собрания, Рейды
"""

import discord
from discord.ext import commands, tasks
import json
import logging
import asyncio
import aiosqlite
import os
from datetime import datetime, timedelta, time
from pathlib import Path
from typing import Optional, Dict, List, Any
import pytz

# ============================================
# УТИЛИТЫ ФОРМАТИРОВАНИЯ ДАТ
# ============================================

def format_date(date_obj=None, date_str=None) -> str:
    """Форматирует дату в DD-MM-YYYY"""
    if date_obj:
        return date_obj.strftime('%d-%m-%Y')
    if date_str:
        try:
            dt = datetime.strptime(date_str, '%Y-%m-%d')
            return dt.strftime('%d-%m-%Y')
        except:
            return date_str
    return datetime.now().strftime('%d-%m-%Y')

def parse_date(date_str: str) -> Optional[datetime]:
    """Парсит дату из DD-MM-YYYY или YYYY-MM-DD"""
    for fmt in ('%d-%m-%Y', '%d.%m.%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None

def date_for_db(date_obj=None) -> str:
    """Возвращает дату в формате для БД (YYYY-MM-DD)"""
    if date_obj:
        return date_obj.strftime('%Y-%m-%d')
    return datetime.now().strftime('%Y-%m-%d')

def format_duration(seconds: int) -> str:
    """Форматирует длительность в читаемый вид"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}ч {minutes}м"
    elif minutes > 0:
        return f"{minutes}м {secs}с"
    else:
        return f"{secs}с"

# ============================================
# КОНФИГУРАЦИЯ И ЛОГИРОВАНИЕ
# ============================================

def load_config():
    """Загружает базовую конфигурацию"""
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logging.critical("❌ config.json не найден!")
        raise SystemExit(1)
    except json.JSONDecodeError as e:
        logging.critical(f"❌ Ошибка парсинга config.json: {e}")
        raise SystemExit(1)

config = load_config()

# Настройка логирования
logging.basicConfig(
    level=getattr(logging, config.get('LOG_LEVEL', 'INFO')),
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('ClanBot')


# ============================================
# ОСНОВНОЙ КЛАСС БОТА
# ============================================

class ClanBot(commands.Bot):
    """Основной класс бота v3.0"""
    
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        intents.guilds = True
        
        super().__init__(
            command_prefix=config.get('PREFIX', '!'),
            intents=intents,
            help_command=None
        )
        
        self.config = config
        self.db: aiosqlite.Connection = None
        self.start_time = datetime.now()
        self.timezone = pytz.timezone(config.get('TIMEZONE', 'Europe/Moscow'))
        
        # Кэш настроек гильдий
        self.guild_settings: Dict[int, Dict] = {}
        self.guild_schedules: Dict[int, List[Dict]] = {}  # Расписание КВ
        self.guild_roles: Dict[int, Dict] = {}
        
        # Активные сессии (ВСЕ голосовые каналы)
        self.all_voice_sessions: Dict[str, Dict] = {}  # {"guild:user": {join_time, channel_id, ...}}
        
        # Активные рейды
        self.active_raids: Dict[int, Dict] = {}
        self.raid_sessions: Dict[str, Dict] = {}
        
        # Активные собрания
        self.active_meetings: Dict[int, Dict] = {}  # {guild_id: {channel_id, start_time, ...}}
    
    async def setup_hook(self):
        """Инициализация при запуске"""
        logger.info("🔧 Инициализация бота v3.0...")
        
        Path('data').mkdir(exist_ok=True)
        Path('cogs').mkdir(exist_ok=True)
        
        await self.init_database()
        
        cogs = ['cogs.attendance', 'cogs.stalcraft', 'cogs.admin']
        for cog in cogs:
            try:
                await self.load_extension(cog)
                logger.info(f"✅ Загружен: {cog}")
            except Exception as e:
                logger.error(f"❌ Ошибка загрузки {cog}: {e}")
        
        logger.info("🔧 Setup завершён")
    
    async def init_database(self):
        """Создание структуры БД v3.0"""
        self.db = await aiosqlite.connect(config.get('DB_PATH', 'data/bot_database.db'))
        
        await self.db.executescript('''
            -- ========================================
            -- НАСТРОЙКИ ГИЛЬДИЙ
            -- ========================================
            
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                kv_vc_channel_id INTEGER DEFAULT NULL,      -- VC для КВ
                meeting_vc_channel_id INTEGER DEFAULT NULL, -- VC для собраний
                report_channel_id INTEGER DEFAULT NULL,     -- Канал отчётов
                log_channel_id INTEGER DEFAULT NULL,        -- Канал логов
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- Роли сервера
            CREATE TABLE IF NOT EXISTS guild_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                role_type TEXT NOT NULL,
                role_id INTEGER NOT NULL,
                role_name TEXT,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, role_type, role_id)
            );
            
            -- ========================================
            -- РАСПИСАНИЕ КВ
            -- ========================================
            
            CREATE TABLE IF NOT EXISTS kv_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT DEFAULT 'КВ',
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                days_of_week TEXT DEFAULT '0,1,2,3,4,5,6',
                active BOOLEAN DEFAULT 1,
                notify_before INTEGER DEFAULT 15,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- ========================================
            -- СЕССИИ - ВСЕ ГОЛОСОВЫЕ КАНАЛЫ
            -- ========================================
            
            CREATE TABLE IF NOT EXISTS voice_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                display_name TEXT,
                channel_id INTEGER NOT NULL,
                channel_name TEXT,
                join_time TIMESTAMP NOT NULL,
                leave_time TIMESTAMP,
                duration_seconds INTEGER DEFAULT 0,
                date TEXT NOT NULL,
                status TEXT DEFAULT 'completed'
            );
            
            -- ========================================
            -- ПОСЕЩАЕМОСТЬ КВ
            -- ========================================
            
            CREATE TABLE IF NOT EXISTS kv_attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                schedule_id INTEGER,
                date TEXT NOT NULL,
                kv_time TEXT,  -- "20:00-21:30"
                user_id INTEGER NOT NULL,
                discord_name TEXT,
                role_type TEXT DEFAULT 'member',
                present BOOLEAN DEFAULT 0,
                excused TEXT DEFAULT NULL,  -- 'У/П' или NULL
                reason TEXT DEFAULT NULL,
                vc_time_seconds INTEGER DEFAULT 0,
                processed_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, date, user_id, schedule_id)
            );
            
            -- ========================================
            -- СОБРАНИЯ
            -- ========================================
            
            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER,
                channel_name TEXT,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                status TEXT DEFAULT 'active',
                created_by INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS meeting_attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                discord_name TEXT,
                role_type TEXT DEFAULT 'member',
                present BOOLEAN DEFAULT 0,
                join_time TIMESTAMP,
                leave_time TIMESTAMP,
                duration_seconds INTEGER DEFAULT 0,
                reason TEXT,
                FOREIGN KEY (meeting_id) REFERENCES meetings(id),
                UNIQUE(meeting_id, user_id)
            );
            
            -- ========================================
            -- РЕЙДЫ И АКТИВНОСТИ
            -- ========================================
            
            CREATE TABLE IF NOT EXISTS raids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                raid_type TEXT DEFAULT 'raid',
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                end_time TEXT,
                vc_channel_id INTEGER,
                description TEXT,
                status TEXT DEFAULT 'planned',
                created_by INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS raid_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raid_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                display_name TEXT,
                join_time TIMESTAMP,
                leave_time TIMESTAMP,
                duration_seconds INTEGER DEFAULT 0,
                status TEXT DEFAULT 'registered',
                FOREIGN KEY (raid_id) REFERENCES raids(id),
                UNIQUE(raid_id, user_id)
            );
            
            -- ========================================
            -- ЛОГИ ДЕЙСТВИЙ
            -- ========================================
            
            CREATE TABLE IF NOT EXISTS action_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                actor_id INTEGER,
                target_id INTEGER,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- Индексы
            CREATE INDEX IF NOT EXISTS idx_voice_sessions_guild_date ON voice_sessions(guild_id, date);
            CREATE INDEX IF NOT EXISTS idx_kv_attendance_guild_date ON kv_attendance(guild_id, date);
            CREATE INDEX IF NOT EXISTS idx_raids_guild ON raids(guild_id);
        ''')
        await self.db.commit()
        logger.info("✅ База данных v3.0 инициализирована")
        
        await self.load_guild_cache()
    
    async def load_guild_cache(self):
        """Загружает настройки в кэш"""
        # Настройки
        async with self.db.execute('SELECT * FROM guild_settings') as cursor:
            async for row in cursor:
                self.guild_settings[row[0]] = {
                    'kv_vc_channel_id': row[1],
                    'meeting_vc_channel_id': row[2],
                    'report_channel_id': row[3],
                    'log_channel_id': row[4]
                }
        
        # Расписание КВ
        async with self.db.execute('SELECT * FROM kv_schedule WHERE active = 1') as cursor:
            async for row in cursor:
                guild_id = row[1]
                if guild_id not in self.guild_schedules:
                    self.guild_schedules[guild_id] = []
                self.guild_schedules[guild_id].append({
                    'id': row[0],
                    'name': row[2],
                    'start_time': row[3],
                    'end_time': row[4],
                    'days_of_week': [int(d) for d in row[5].split(',')],
                    'notify_before': row[7]
                })
        
        # Роли
        async with self.db.execute('SELECT * FROM guild_roles') as cursor:
            async for row in cursor:
                guild_id = row[1]
                role_type = row[2]
                role_id = row[3]
                
                if guild_id not in self.guild_roles:
                    self.guild_roles[guild_id] = {}
                if role_type not in self.guild_roles[guild_id]:
                    self.guild_roles[guild_id][role_type] = []
                self.guild_roles[guild_id][role_type].append(role_id)
        
        logger.info(f"📦 Загружен кэш: {len(self.guild_settings)} гильдий")
    
    def get_current_kv_schedule(self, guild_id: int) -> Optional[Dict]:
        """Проверяет, идёт ли сейчас КВ"""
        if guild_id not in self.guild_schedules:
            return None
        
        now = datetime.now(self.timezone)
        current_day = now.weekday()
        current_time = now.time()
        
        for schedule in self.guild_schedules[guild_id]:
            if current_day not in schedule['days_of_week']:
                continue
            
            start = datetime.strptime(schedule['start_time'], '%H:%M').time()
            end = datetime.strptime(schedule['end_time'], '%H:%M').time()
            
            if start <= end:
                if start <= current_time <= end:
                    return schedule
            else:
                if current_time >= start or current_time <= end:
                    return schedule
        
        return None
    
    def get_member_role_type(self, member: discord.Member) -> str:
        """Определяет тип роли участника"""
        guild_id = member.guild.id
        if guild_id not in self.guild_roles:
            return 'member'
        
        user_role_ids = [role.id for role in member.roles]
        
        for role_type in ['leader', 'officer', 'special', 'member', 'recruit']:
            if role_type in self.guild_roles[guild_id]:
                for role_id in self.guild_roles[guild_id][role_type]:
                    if role_id in user_role_ids:
                        return role_type
        
        return 'member'
    
    async def has_permission(self, member: discord.Member, permission_level: str) -> bool:
        """Проверяет права пользователя"""
        if member.guild_permissions.administrator:
            return True
        
        guild_id = member.guild.id
        if guild_id not in self.guild_roles:
            return False
        
        user_role_ids = [role.id for role in member.roles]
        hierarchy = ['leader', 'officer', 'special', 'member', 'recruit']
        
        try:
            required_level = hierarchy.index(permission_level)
        except ValueError:
            return False
        
        for role_type in hierarchy[:required_level + 1]:
            if role_type in self.guild_roles[guild_id]:
                for role_id in self.guild_roles[guild_id][role_type]:
                    if role_id in user_role_ids:
                        return True
        
        return False
    
    async def send_log(self, guild: discord.Guild, embed: discord.Embed):
        """Отправляет лог в канал логов"""
        settings = self.guild_settings.get(guild.id, {})
        log_channel_id = settings.get('log_channel_id')
        
        if log_channel_id:
            channel = guild.get_channel(log_channel_id)
            if channel:
                try:
                    await channel.send(embed=embed)
                except Exception as e:
                    logger.error(f"Ошибка отправки лога: {e}")
    
    async def on_ready(self):
        """Событие готовности"""
        logger.info(f"{'='*50}")
        logger.info(f"🎮 Бот запущен: {self.user.name} ({self.user.id})")
        logger.info(f"📡 Подключён к {len(self.guilds)} серверам")
        logger.info(f"{'='*50}")
        
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="за кланом | !help"
            )
        )
        
        if not self.schedule_checker.is_running():
            self.schedule_checker.start()
        
        try:
            synced = await self.tree.sync()
            logger.info(f"✅ Синхронизировано {len(synced)} slash-команд")
        except Exception as e:
            logger.error(f"❌ Ошибка синхронизации: {e}")
    
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ):
        """
        Трекинг ВСЕХ голосовых каналов
        Записывает каждый вход/выход в любой VC
        """
        if member.bot:
            return
        
        guild_id = member.guild.id
        now = datetime.now(self.timezone)
        today = date_for_db(now)
        session_key = f"{guild_id}:{member.id}"
        
        settings = self.guild_settings.get(guild_id, {})
        kv_vc_id = settings.get('kv_vc_channel_id')
        
        # ===== ВХОД В ЛЮБОЙ ГОЛОСОВОЙ КАНАЛ =====
        if after.channel and (before.channel is None or before.channel.id != after.channel.id):
            
            # Если был в другом канале - закрываем старую сессию
            if session_key in self.all_voice_sessions:
                old_session = self.all_voice_sessions.pop(session_key)
                duration = (now - old_session['join_time']).total_seconds()
                
                await self.db.execute('''
                    UPDATE voice_sessions SET leave_time = ?, duration_seconds = ?, status = 'completed'
                    WHERE guild_id = ? AND user_id = ? AND status = 'active'
                ''', (now.isoformat(), int(duration), guild_id, member.id))
            
            # Создаём новую сессию
            self.all_voice_sessions[session_key] = {
                'join_time': now,
                'channel_id': after.channel.id,
                'channel_name': after.channel.name,
                'guild_id': guild_id
            }
            
            await self.db.execute('''
                INSERT INTO voice_sessions 
                (guild_id, user_id, username, display_name, channel_id, channel_name, join_time, date, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
            ''', (
                guild_id, member.id, str(member), member.display_name,
                after.channel.id, after.channel.name, now.isoformat(), today
            ))
            await self.db.commit()
            
            logger.info(f"🎤 ВХОД | {member.display_name} → {after.channel.name}")
            
            # Лог в канал
            log_embed = discord.Embed(
                title="🎤 Вход в голосовой канал",
                description=f"**{member.display_name}** зашёл в **{after.channel.name}**",
                color=discord.Color.green(),
                timestamp=now
            )
            log_embed.set_thumbnail(url=member.display_avatar.url)
            await self.send_log(member.guild, log_embed)
            
            # Проверяем КВ
            if kv_vc_id and after.channel.id == kv_vc_id:
                current_kv = self.get_current_kv_schedule(guild_id)
                if current_kv:
                    role_type = self.get_member_role_type(member)
                    await self.db.execute('''
                        INSERT INTO kv_attendance 
                        (guild_id, schedule_id, date, kv_time, user_id, discord_name, role_type, present)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                        ON CONFLICT(guild_id, date, user_id, schedule_id) DO UPDATE SET
                            present = 1, role_type = excluded.role_type
                    ''', (
                        guild_id, current_kv['id'], today,
                        f"{current_kv['start_time']}-{current_kv['end_time']}",
                        member.id, member.display_name, role_type
                    ))
                    await self.db.commit()
            
            # Проверяем рейды
            await self._handle_raid_join(member, after.channel, now)
            
            # Проверяем собрания
            if guild_id in self.active_meetings:
                meeting = self.active_meetings[guild_id]
                if after.channel.id == meeting['channel_id']:
                    role_type = self.get_member_role_type(member)
                    await self.db.execute('''
                        INSERT INTO meeting_attendance 
                        (meeting_id, guild_id, user_id, discord_name, role_type, present, join_time)
                        VALUES (?, ?, ?, ?, ?, 1, ?)
                        ON CONFLICT(meeting_id, user_id) DO UPDATE SET
                            present = 1, join_time = COALESCE(meeting_attendance.join_time, excluded.join_time)
                    ''', (meeting['id'], guild_id, member.id, member.display_name, role_type, now.isoformat()))
                    await self.db.commit()
        
        # ===== ВЫХОД ИЗ ГОЛОСОВОГО КАНАЛА =====
        if before.channel and (after.channel is None or before.channel.id != after.channel.id):
            
            if session_key in self.all_voice_sessions:
                session = self.all_voice_sessions.pop(session_key)
                duration = (now - session['join_time']).total_seconds()
                
                await self.db.execute('''
                    UPDATE voice_sessions SET leave_time = ?, duration_seconds = ?, status = 'completed'
                    WHERE guild_id = ? AND user_id = ? AND status = 'active'
                ''', (now.isoformat(), int(duration), guild_id, member.id))
                await self.db.commit()
                
                logger.info(f"🎤 ВЫХОД | {member.display_name} ← {before.channel.name} | {format_duration(int(duration))}")
                
                # Лог
                log_embed = discord.Embed(
                    title="🎤 Выход из голосового канала",
                    description=f"**{member.display_name}** вышел из **{before.channel.name}**\n"
                               f"⏱️ Время: **{format_duration(int(duration))}**",
                    color=discord.Color.orange(),
                    timestamp=now
                )
                await self.send_log(member.guild, log_embed)
                
                # Обновляем время в КВ
                if kv_vc_id and before.channel.id == kv_vc_id:
                    await self.db.execute('''
                        UPDATE kv_attendance SET vc_time_seconds = vc_time_seconds + ?
                        WHERE guild_id = ? AND date = ? AND user_id = ?
                    ''', (int(duration), guild_id, today, member.id))
                    await self.db.commit()
                
                # Рейды
                await self._handle_raid_leave(member, before.channel, now, duration)
                
                # Собрания
                if guild_id in self.active_meetings:
                    meeting = self.active_meetings[guild_id]
                    if before.channel.id == meeting['channel_id']:
                        await self.db.execute('''
                            UPDATE meeting_attendance 
                            SET leave_time = ?, duration_seconds = duration_seconds + ?
                            WHERE meeting_id = ? AND user_id = ?
                        ''', (now.isoformat(), int(duration), meeting['id'], member.id))
                        await self.db.commit()
    
    async def _handle_raid_join(self, member: discord.Member, channel: discord.VoiceChannel, now: datetime):
        """Обработка входа в рейдовый канал"""
        async with self.db.execute('''
            SELECT id, name, date FROM raids
            WHERE guild_id = ? AND status = 'active' AND vc_channel_id = ?
        ''', (member.guild.id, channel.id)) as cursor:
            raid = await cursor.fetchone()
        
        if raid:
            raid_id, raid_name, raid_date = raid
            session_key = f"{raid_id}:{member.id}"
            
            self.raid_sessions[session_key] = {
                'join_time': now,
                'raid_id': raid_id,
                'raid_name': raid_name,
                'raid_date': raid_date
            }
            
            await self.db.execute('''
                INSERT INTO raid_participants (raid_id, user_id, username, display_name, join_time, status)
                VALUES (?, ?, ?, ?, ?, 'joined')
                ON CONFLICT(raid_id, user_id) DO UPDATE SET
                    join_time = COALESCE(raid_participants.join_time, excluded.join_time),
                    status = 'joined'
            ''', (raid_id, member.id, str(member), member.display_name, now.isoformat()))
            await self.db.commit()
            
            logger.info(f"⚔️ РЕЙД ВХОД | {member.display_name} | {raid_name}")
    
    async def _handle_raid_leave(self, member: discord.Member, channel: discord.VoiceChannel, now: datetime, duration: float):
        """Обработка выхода из рейдового канала"""
        async with self.db.execute('''
            SELECT id, name, date FROM raids
            WHERE guild_id = ? AND status = 'active' AND vc_channel_id = ?
        ''', (member.guild.id, channel.id)) as cursor:
            raid = await cursor.fetchone()
        
        if raid:
            raid_id, raid_name, raid_date = raid
            session_key = f"{raid_id}:{member.id}"
            
            session = self.raid_sessions.pop(session_key, None)
            if session:
                await self.db.execute('''
                    UPDATE raid_participants 
                    SET leave_time = ?, duration_seconds = duration_seconds + ?, status = 'left'
                    WHERE raid_id = ? AND user_id = ?
                ''', (now.isoformat(), int(duration), raid_id, member.id))
                await self.db.commit()
                
                logger.info(f"⚔️ РЕЙД ВЫХОД | {member.display_name} | {raid_name} | {format_duration(int(duration))}")
    
    @tasks.loop(minutes=1)
    async def schedule_checker(self):
        """Проверка расписания КВ и уведомления"""
        now = datetime.now(self.timezone)
        current_day = now.weekday()
        
        for guild_id, schedules in self.guild_schedules.items():
            guild = self.get_guild(guild_id)
            if not guild:
                continue
            
            settings = self.guild_settings.get(guild_id, {})
            report_channel_id = settings.get('report_channel_id')
            
            for schedule in schedules:
                if current_day not in schedule['days_of_week']:
                    continue
                
                start_time = datetime.strptime(schedule['start_time'], '%H:%M').time()
                end_time = datetime.strptime(schedule['end_time'], '%H:%M').time()
                notify_before = schedule.get('notify_before', 15)
                
                # Уведомление о начале КВ
                notify_time = (datetime.combine(now.date(), start_time) - timedelta(minutes=notify_before)).time()
                
                if now.hour == notify_time.hour and now.minute == notify_time.minute:
                    if report_channel_id:
                        channel = guild.get_channel(report_channel_id)
                        if channel:
                            embed = discord.Embed(
                                title=f"⚔️ КВ через {notify_before} минут!",
                                description=f"**{schedule['name']}**\n"
                                           f"🕐 Время: **{schedule['start_time']} - {schedule['end_time']}**",
                                color=discord.Color.red(),
                                timestamp=now
                            )
                            
                            # Упоминаем роли
                            mentions = []
                            if guild_id in self.guild_roles:
                                for role_type in ['member', 'recruit', 'officer', 'leader']:
                                    if role_type in self.guild_roles[guild_id]:
                                        for role_id in self.guild_roles[guild_id][role_type]:
                                            role = guild.get_role(role_id)
                                            if role:
                                                mentions.append(role.mention)
                            
                            try:
                                await channel.send(content=" ".join(mentions) if mentions else None, embed=embed)
                            except Exception as e:
                                logger.error(f"Ошибка уведомления КВ: {e}")
                
                # Уведомление о завершении КВ
                if now.hour == end_time.hour and now.minute == end_time.minute:
                    if report_channel_id:
                        channel = guild.get_channel(report_channel_id)
                        if channel:
                            today = date_for_db(now)
                            
                            # Статистика КВ
                            async with self.db.execute('''
                                SELECT COUNT(*), SUM(vc_time_seconds)
                                FROM kv_attendance
                                WHERE guild_id = ? AND date = ? AND schedule_id = ? AND present = 1
                            ''', (guild_id, today, schedule['id'])) as cursor:
                                stats = await cursor.fetchone()
                            
                            present = stats[0] or 0
                            total_time = stats[1] or 0
                            avg_time = int(total_time / present / 60) if present > 0 else 0
                            
                            embed = discord.Embed(
                                title="🏁 КВ завершена!",
                                description=f"**{schedule['name']}**",
                                color=discord.Color.green(),
                                timestamp=now
                            )
                            embed.add_field(name="👥 Участников", value=str(present), inline=True)
                            embed.add_field(name="⏱️ Среднее время", value=f"{avg_time} мин", inline=True)
                            embed.add_field(name="📅 Дата", value=format_date(now), inline=True)
                            embed.set_footer(text="Используйте !kv для полного отчёта")
                            
                            try:
                                await channel.send(embed=embed)
                            except Exception as e:
                                logger.error(f"Ошибка уведомления завершения КВ: {e}")
    
    @schedule_checker.before_loop
    async def before_schedule_checker(self):
        await self.wait_until_ready()
    
    async def close(self):
        """Корректное закрытие"""
        logger.info("🛑 Завершение работы...")
        
        now = datetime.now(self.timezone)
        for session_key, session in self.all_voice_sessions.items():
            try:
                duration = (now - session['join_time']).total_seconds()
                await self.db.execute('''
                    UPDATE voice_sessions 
                    SET leave_time = ?, duration_seconds = ?, status = 'interrupted'
                    WHERE guild_id = ? AND user_id = ? AND status = 'active'
                ''', (now.isoformat(), int(duration), session['guild_id'], int(session_key.split(':')[1])))
            except Exception as e:
                logger.error(f"Ошибка при закрытии голосовой сессии {session_key}: {e}")

        if self.db:
            await self.db.commit()
            await self.db.close()
        
        await super().close()


# ============================================
# КОМАНДА HELP
# ============================================

@commands.command(name='help', aliases=['помощь', 'h'])
async def help_command(ctx: commands.Context):
    """Показывает справку по командам"""
    
    embed = discord.Embed(
        title="📖 STALCRAFT Clan Bot v3.2",
        description="Полный список команд",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="⚙️ Настройка (Админ)",
        value="`!setup` - Мастер настройки\n"
              "`!setvc kv #канал` - VC для КВ\n"
              "`!setvc meeting #канал` - VC для собраний\n"
              "`!setchannel log #канал` - Канал логов\n"
              "`!setchannel report #канал` - Канал отчётов",
        inline=False
    )
    
    embed.add_field(
        name="👑 Роли (Админ)",
        value="`!setrole leader @роль`\n"
              "`!setrole officer @роль`\n"
              "`!setrole member @роль`\n"
              "`!roles` - Показать роли",
        inline=False
    )
    
    embed.add_field(
        name="⚔️ КВ - Клановые войны",
        value="`!schedule add 20:00-21:30 КВ пн,ср,пт` - Расписание\n"
              "`!schedule list` - Список расписаний\n"
              "`!kv` - Кто сейчас в VC (только во время КВ!)\n"
              "`!kv 17-01-2026` - Отчёт за конкретную дату\n"
              "`!kvedit` - Редактировать посещаемость (меню)\n"
              "`!kvedit 17-01-2026 @user присутствовал`",
        inline=False
    )
    
    embed.add_field(
        name="📋 Собрания",
        value="`!meeting start` - Начать собрание (зайди в VC)\n"
              "`!meeting plan 20:00 18-01-2026 Название` - Запланировать\n"
              "`!meeting list` - Список собраний\n"
              "`!meeting end` - Завершить\n"
              "`!meeting cancel <ID>` - Отменить",
        inline=False
    )
    
    embed.add_field(
        name="🎯 Активности (Gold Drop, Рейды)",
        value="`!activity new` - Создать через меню\n"
              "`!activity quick 20:00 17-01-2026 Gold Drop`\n"
              "`!activity list` - Список\n"
              "`!activity start <ID>` / `!activity end <ID>`\n"
              "`!calendar` - Календарь на неделю",
        inline=False
    )
    
    embed.add_field(
        name="📊 Статистика",
        value="`!me` - Своя статистика (КВ, собрания, активности)\n"
              "`!me @user` - Статистика другого (офицеры+)\n"
              "`!stats @user` - То же что !me\n"
              "`!top10` - Топ-10 по КВ\n"
              "`!online` - Кто сейчас в голосовых\n"
              "`!export` - Экспорт в Excel (5 листов)",
        inline=False
    )
    
    embed.set_footer(text="STALCRAFT Clan Bot v3.1 | Все даты в формате DD-MM-YYYY")
    await ctx.send(embed=embed)


# ============================================
# ЗАПУСК
# ============================================

async def main():
    bot = ClanBot()
    bot.add_command(help_command)
    
    try:
        async with bot:
            await bot.start(config['TOKEN'])
    except discord.LoginFailure:
        logger.critical("❌ Неверный токен!")
    except Exception as e:
        logger.critical(f"❌ Критическая ошибка: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
