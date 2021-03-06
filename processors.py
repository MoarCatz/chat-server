import psycopg2, psycopg2.extras
import json, re, os, binascii
import rsa, rsa.pkcs1, pyaes
from urllib.parse import urlparse
from datetime import datetime
from hashlib import md5
from random import randint
from base64 import b64encode, b64decode
from itertools import chain


class BadRequest(Exception):
    """Класс исключений для индикации логической ошибки в запросе"""


class ClientCodes():
    """Перечисление кодов запросов от клиента"""
    register = 0
    login = 1
    get_search_list = 2
    friends_group = 3
    get_message_history = 4
    send_message = 5
    change_profile_section = 6
    add_to_blacklist = 7
    delete_from_friends = 8
    send_request = 9
    delete_profile = 10
    logout = 11
    create_dialog = 12
    get_profile_info = 13
    remove_from_blacklist = 14
    take_request_back = 15
    confirm_add_request = 16
    add_to_favorites = 17
    search_msg = 18
    remove_from_favorites = 19
    get_add_requests = 20
    decline_add_request = 21
    set_image = 22


class ServerCodes():
    """Перечисление кодов запросов от сервера"""
    login_error = 0
    register_error = 1
    login_succ = 2
    register_succ = 3
    search_list = 4
    friends_group_response = 5
    message_history = 6
    message_received = 7
    new_message = 8
    change_profile_section_succ = 9
    friends_group_update = 10
    add_to_blacklist_succ = 11
    delete_from_friends_succ = 12
    send_request_succ = 13
    new_add_request = 14
    add_request_confirm = 15
    delete_profile_succ = 16
    logout_succ = 17
    create_dialog_succ = 18
    profile_info = 19
    remove_from_blacklist_succ = 20
    take_request_back_succ = 21
    confirm_add_request_succ = 22
    add_to_favorites_succ = 23
    add_request_decline = 24
    search_msg_result = 25
    remove_from_favorites_succ = 26
    add_requests = 27
    decline_add_request_succ = 28
    set_image_succ = 29


cc = ClientCodes
sc = ServerCodes


class Processor:
    # Парсинг ссылки на базу данных
    url = urlparse(os.environ["DATABASE_URL"])

    db = psycopg2.connect(database = url.path[1:],
                          user = url.username,
                          password = url.password,
                          host = url.hostname,
                          port = url.port,
                          cursor_factory = psycopg2.extras.DictCursor)

    # Получение приватного ключа
    c = db.cursor()
    c.execute('''SELECT * FROM key''')
    key_pair = c.fetchone()
    priv_key = rsa.PrivateKey(*list(map(int, key_pair['priv_key'])))
    pub_key_str = ':'.join(key_pair['pub_key'])
    c.close()
    del c, key_pair

    # Регулярное выражение для валидации имен пользователей
    nick_ptrn = re.compile('(?![ ]+)[\w ]{2,15}')

    def _request_id(self):
        """Генерирует случайный идентификатор запроса"""
        return md5(str(randint(0, 10000000)).encode()).hexdigest().encode()

    def _send_notification(self, user, code, conns):
        """Отправляет пользователю user уведомление с кодом code
        Если user не в сети, уведомления он не получает"""
        ntf = str(code).encode()
        c = self.db.cursor()
        c.execute('''SELECT ip FROM sessions
                     WHERE name = %s''', (user,))
        row = c.fetchone()
        if not row:
            return
        ip = row['ip']
        if ip in conns:
            conns[ip].write_message(ntf, binary = True)

    def _get_public_key(self, ip):
        """Получает публичный ключ для сессии, открытой с IP-адреса ip
        Вызывает BadRequest, если нет сессий, открытых с этого IP-адреса"""
        c = self.db.cursor()
        c.execute('''SELECT pub_key FROM sessions
                     WHERE ip = %s''', (ip,))
        key = c.fetchone()
        if not key:
            raise BadRequest
        n, e = key['pub_key']
        pub_key = rsa.PublicKey(int(n), int(e))
        return pub_key

    def _decrypt(self, request, enc_key):
        """Расшифровывает байт-строку request в base64 через ключ enc_key
        Вызывает BadRequest, если расшифровать строку не удалось"""
        try:
            decoded_request = b64decode(request)
            decoded_key = b64decode(enc_key)
            key = rsa.decrypt(decoded_key, self.priv_key)
        except (rsa.pkcs1.DecryptionError, binascii.Error):
            raise BadRequest

        aes = pyaes.AESModeOfOperationCTR(key)
        decrypted = aes.decrypt(decoded_request)

        return decrypted

    def _encrypt(self, response, pub_key):
        """Зашифровывает байт-строку response AES-шифрованием, а ключ
        шифрует публичным ключом клиента pub_key"""
        key = os.urandom(32)
        aes = pyaes.AESModeOfOperationCTR(key)
        enc_response = aes.encrypt(response)
        enc_key = rsa.encrypt(key, pub_key)

        return b64encode(enc_response) + b':' + b64encode(enc_key)

    def _verify_signature(self, request, signature, pub_key):
        """Проверяет подлинность подписи signature байт-строки request в base64
        публичным ключом pub_key
        Вызывает BadRequest, если проверка не пройдена"""
        decoded_request = b64decode(request)
        try:
            rsa.verify(decoded_request, signature, pub_key)
        except rsa.pkcs1.VerificationError:
            raise BadRequest

    def _add_session(self, nick, pub_key, ip):
        """Добавляет пользователя nick по IP-адресу ip
        с публичным ключом pub_key в таблицу сессий
        Вызывает BadRequest, если запись с таким публичным
        ключом уже есть в таблице"""
        c = self.db.cursor()

        try:
            with self.db:
                c.execute('''INSERT INTO sessions
                             VALUES (%s, %s, %s)''',
                          (nick, pub_key.split(':'), ip))
        except psycopg2.IntegrityError:
            raise BadRequest
        c.close()

    def _get_nick(self, ip):
        """Возвращает имя пользователя, чья сессия открыта с IP-адреса ip
        Вызывает BadRequest, если такой записи нет"""
        c = self.db.cursor()
        c.execute('''SELECT name FROM sessions
                     WHERE ip = %s''', (ip,))
        nick = c.fetchone()
        c.close()
        if nick:
            return nick['name']
        raise BadRequest

    def _pack(self, *data):
        """Собирает данные data в формат для передачи
        Возвращает отформатированную байт-строку"""
        return json.dumps(data, separators = (',', ':'))[1:-1].encode()

    def _close_session(self, ip):
        """Удаляет из таблицы сессий запись, открытую с IP-адреса ip"""
        c = self.db.cursor()
        with self.db:
            c.execute('''DELETE FROM sessions
                         WHERE ip = %s''', (ip,))

        c.close()

    def _remove_from(self, nick, item, sect):
        """Удаляет элемент item из графы sect в записи с именем nick
        Вызывает BadRequest, если пользователь nick не найден
        """
        c = self.db.cursor()
        c.execute('''SELECT {}::text[] FROM users
                     WHERE name = %s'''.format(sect), (nick,))

        prev = c.fetchone()
        if not prev:
            raise BadRequest
        data = prev[sect]
        try:
            data.remove(item)
        except ValueError:
            pass  # Если элемента нет, проигнорировать исключение
        with self.db:
            c.execute('''UPDATE users SET {} = %s
                         WHERE name = %s'''.format(sect), (data, nick))

        c.close()

    def _is_blacklisted(self, nick, user):
        """Проверяет, находится ли nick в черном списке user"""
        c = self.db.cursor()
        if nick == user:
            return False
        c.execute('''SELECT name FROM users
                     WHERE name = %s AND %s = ANY(blacklist::text[])''',
                  (user, nick))
        return bool(c.fetchone())

    def _remove_add_request(self, nick, user):
        """Удаляет запрос от nick к user"""
        c = self.db.cursor()
        with self.db:
            c.execute('''DELETE FROM requests
                         WHERE from_who = %s AND to_who = %s''',
                      (nick, user))

        c.close()

    def _add_to(self, nick, item, sect):
        """Добавляет элемент item к графе sect в записи с именем nick
        Вызывает BadRequest, если пользователь nick не найден"""
        c = self.db.cursor()

        c.execute('''SELECT {}::text[] FROM users
                     WHERE name = %s'''.format(sect), (nick,))

        prev = c.fetchone()
        if not prev:
            raise BadRequest
        data = prev[sect]
        if item in data:
            return  # Если элемент уже есть, не добавлять его еще раз
        data.append(item)
        with self.db:
            c.execute('''UPDATE users SET {} = %s
                         WHERE name = %s'''.format(sect),
                      (data, nick))

        c.close()

    def _get_collocutor(self, dialog, nick):
        """Возвращает собеседника nick в диалоге dialog
        Вызывает BadRequest, если собеседника нет"""
        c = self.db.cursor()

        c.execute('''SELECT name FROM users
                     WHERE name != %s AND %s = ANY(dialogs::text[])''',
                  (nick, str(dialog)))
        row = c.fetchone()
        if not row:
            return None
        return row['name']

    def _user_in_dialog(self, user, dialog):
        """Проверяет, что диалог под номером dialog есть
        в графе диалогов пользователя user
        Вызывает BadRequest, если пользователь не найден,
        диалога dialog нет в графе или dialog не является целым числом"""
        if not isinstance(dialog, int):
            raise BadRequest
        c = self.db.cursor()
        c.execute('''SELECT dialogs FROM users
                     WHERE name = %s AND %s = ANY(dialogs::text[])''',
                  (user, str(dialog)))
        if not c.fetchone():
            raise BadRequest

        c.close()

    def _delete_dialog(self, dialog, user):
        """Удаляет диалог под номером dialog по запросу пользователя user
        Если собеседник удалил для себя этот диалог, таблица диалога удаляется.
        Иначе пользователь, от кого поступил запрос на удаление,
        помечается как удаливший этот диалог для себя"""
        c = self.db.cursor()
        c.execute('''SELECT sender FROM d{}
                     WHERE sender != %s'''.format(dialog), (user,))
        sender = c.fetchone()
        if not sender or sender['sender'][0] == '~':
            c.execute('''DROP TABLE d{}'''.format(dialog))
        else:
            c.execute('''UPDATE d{} SET sender = '~' || %s
                         WHERE sender = %s'''.format(dialog),
                      (user, user))
        # Диалог с номером dialog удаляется из диалогов пользователя user
        self._remove_from(user, str(dialog), 'dialogs')
        self.db.commit()

        c.close()

    def _user_exists(self, user):
        """Проверяет, что пользователь user существует
        Вызывает BadRequest в противном случае"""
        c = self.db.cursor()
        c.execute('''SELECT name FROM users
                     WHERE name = %s''', (user,))
        if not c.fetchone():
            raise BadRequest

        c.close()

    def _valid_nick(self, nick):
        """Проверяет, является ли nick допустимым именем пользователя"""
        return bool(re.fullmatch(self.nick_ptrn, nick))

    def _next_free_dialog(self):
        """Возвращает следующий свободный номер диалога"""
        c = self.db.cursor()
        c.execute('''SELECT table_name FROM information_schema.tables
                     WHERE table_schema = 'public' ''')
        dialogs = sorted(int(i['table_name'][1:]) for i in c.fetchall()
                         if i['table_name'][0] == 'd')
        # Здесь из всех таблиц, названия которых начинаются с 'd', будет взят
        # номер, и полученный список номеров будет отсортирован
        c.close()
        if not dialogs:
            # Если еще нет диалогов
            return 1

        for i in range(1, len(dialogs)):
            if dialogs[i] - dialogs[i - 1] != 1:
                return dialogs[i - 1] + 1
        return dialogs[-1] + 1

    def _set_timestamp(self, address):
        """Сохраняет текущую дату в last_active
        таблицы sessions"""
        c = self.db.cursor()
        stamp = datetime.now().timestamp()
        c.execute('''UPDATE sessions SET last_active = %s
                     WHERE ip = %s''', (int(stamp), address))

        c.close()

    def _clean_up(self, address):
        """Закрывает все сессии с address на случай аварийного закрытия
        соединения клиентом"""
        c = self.db.cursor()
        c.execute('''DELETE FROM sessions
                     WHERE ip = %s''', (address,))
        c.close()
        self.db.commit()

    def register(self, request_id, ip, nick, pswd, pub_key):
        """Зарегистрироваться с именем nick, хэшем pswd пароля
        и публичным ключом pub_key"""
        with open('avatar_placeholder.png', 'rb') as f:
            img = f.read()

        if not self._valid_nick(nick):
            return self._pack(sc.register_error, request_id)

        c = self.db.cursor()

        try:
            with self.db:
                c.execute('''INSERT INTO users
                             VALUES (%s, %s,
                                     ARRAY[]::text[],
                                     ARRAY[]::text[],
                                     ARRAY[]::text[],
                                     ARRAY[]::text[])''',
                          (nick, pswd))

                c.execute('''INSERT INTO profiles
                             VALUES (%s, '', '', 0, '', %s)''', (nick, img))
        except psycopg2.IntegrityError:
            # Если пользователь с таким именем существует
            return self._pack(sc.register_error, request_id)

        self._add_session(nick, pub_key, ip)

        c.close()
        return self._pack(sc.register_succ, request_id)

    def login(self, request_id, ip, nick, pswd, pub_key, conns):
        """Войти в систему с именем nick, хэшем pswd пароля
        и публичным ключом pub_key"""
        c = self.db.cursor()
        c.execute('''SELECT name, friends FROM users
                     WHERE name = %s AND password = %s''', (nick, pswd))
        row = c.fetchone()
        if not row:
            # Если такой комбинации имени-пароля нет
            return (self._pack(sc.login_error, request_id),
                    rsa.PublicKey(*list(map(int, pub_key.split(':')))))

        friends = row['friends']

        c.execute('''SELECT name FROM users
                     WHERE %s = ANY(blacklist::text[])''', (nick,))
        in_bl = [row['name'] for row in c.fetchall()]

        c.execute('''SELECT to_who FROM requests
                     WHERE from_who = %s''', (nick,))
        outc = [row['to_who'] for row in c.fetchall()]

        c.execute('''SELECT from_who FROM requests
                     WHERE to_who = %s''', (nick,))
        inc = [row['from_who'] for row in c.fetchall()]

        for i in chain(friends, in_bl, outc, inc):
            self._send_notification(i, sc.friends_group_update, conns)

        try:
            self._add_session(nick, pub_key, ip)
        except BadRequest:
            return (self._pack(sc.login_error, request_id),
                    rsa.PublicKey(*list(map(int, pub_key.split(':')))))

        c.close()
        return self._pack(sc.login_succ, request_id)

    def search_list(self, request_id, ip):
        """Получить список всех пользователей и их статусов для поиска"""
        c = self.db.cursor()
        nick = self._get_nick(ip)
        c.execute('''SELECT name FROM sessions''')
        online = set(row['name'] for row in c.fetchall())

        c.execute('''SELECT from_who FROM requests
                     WHERE to_who = %s''', (nick,))
        inc = set(row['from_who'] for row in c.fetchall())
        c.execute('''SELECT to_who FROM requests
                     WHERE from_who = %s''', (nick,))
        outc = set(row['to_who'] for row in c.fetchall())

        c.execute('''SELECT name FROM users
                     WHERE name != %s AND
                     (%s != ANY(blacklist::text[]) OR blacklist = '{}') AND
                     (%s != ANY(friends::text[]) OR friends = '{}')''', (nick, nick, nick))
        user_list = []
        for row in c.fetchall():
            name = row['name']
            if name not in inc and name not in outc:
                user_list.append((name, name in online))

        c.close()
        return self._pack(sc.search_list, request_id, user_list)

    def friends_group(self, request_id, ip):
        """Получить список друзей, сгрупированных в списки:
        онлайн, оффлайн, избранные, заблокированные"""
        c = self.db.cursor()
        nick = self._get_nick(ip)
        c.execute('''SELECT friends::text[],
                            favorites::text[],
                            blacklist::text[]
                     FROM users WHERE name = %s''', (nick,))
        friends, fav, bl = c.fetchone()

        c.execute('''SELECT name FROM sessions''')
        online_all = {i['name'] for i in c.fetchall()}

        online = []
        offline = []
        for i in friends:
            if i in online_all:
                online.append((i, True))
            else:
                offline.append((i, False))

        fav = [(name, name in online_all) for name in fav]
        bl = [(name, name in online_all) for name in bl]
        c.close()
        return self._pack(sc.friends_group_response, request_id,
                          [fav, online, offline, bl])

    def message_history(self, request_id, ip, count, dialog):
        """Получить count последних сообщений из диалога dialog
        Если count = 0, возвращает все сообщения
        Вызывает BadRequest, если count < 0"""
        c = self.db.cursor()
        if count < 0:
            raise BadRequest

        nick = self._get_nick(ip)
        self._user_in_dialog(nick, dialog)

        c.execute('''SELECT * FROM d{}
                     ORDER BY timestamp'''.format(dialog))
        msgs = [tuple(i) for i in c.fetchall()]
        c.close()
        return self._pack(sc.message_history, request_id, msgs[-count:])

    def send_message(self, request_id, ip, msg, tm, dialog, conns):
        """Отправить сообщение msg с временем tm в диалог под номером dialog
        Вызывает BadRequest, если отправитель находится в черном списке
        собеседника или длина сообщения превышает 1000 символов"""
        if not isinstance(dialog, int):
            raise BadRequest
        max_msg_length = 1000
        if len(msg) > max_msg_length:
            raise BadRequest
        nick = self._get_nick(ip)
        self._user_in_dialog(nick, dialog)

        user = self._get_collocutor(dialog, nick)
        if user and self._is_blacklisted(nick, user):
            raise BadRequest

        c = self.db.cursor()
        with self.db:
            c.execute('''INSERT INTO d{}
                         VALUES (%s, %s, %s)'''.format(dialog),
                      (msg, tm, nick))

        if not user:
            self._send_notification(user, sc.new_message, conns)

        c.close()
        return self._pack(sc.message_received, request_id)

    def change_profile_section(self, request_id, ip, sect, change):
        """Заменить секцию профиля sect на change
        Вызывает BadRequest, если дата рождения (секция 2)
        меняется на что-то кроме целого числа
        или указана несуществующая секция"""
        nick = self._get_nick(ip)

        birthday = 2
        if not isinstance(change, int) and sect == birthday:
            raise BadRequest

        sections = {0: 'status',
                    1: 'email',
                    2: 'birthday',
                    3: 'about'}

        try:
            sect_name = sections[sect]
        except KeyError:
            raise BadRequest

        c = self.db.cursor()
        with self.db:
            c.execute('''UPDATE profiles SET {} = %s
                         WHERE name = %s'''.format(sect_name),
                      (change, nick))
        c.close()
        return self._pack(sc.change_profile_section_succ, request_id)

    def add_to_blacklist(self, request_id, ip, user, conns):
        """Добавить пользователя user в черный список
        Вызывает BadRequest, если отправитель пытается добавить себя"""
        nick = self._get_nick(ip)
        self._user_exists(user)
        if nick == user:
            raise BadRequest
        self._remove_from(nick, user, 'friends')
        self._remove_from(nick, user, 'favorites')
        self._add_to(nick, user, 'blacklist')
        self._remove_add_request(nick, user)
        self._remove_add_request(user, nick)

        self._send_notification(user, sc.friends_group_update, conns)

        return self._pack(sc.add_to_blacklist_succ, request_id)

    def delete_from_friends(self, request_id, ip, user, conns):
        """Удалить пользователя user из друзей"""
        nick = self._get_nick(ip)
        self._user_exists(user)
        self._remove_from(nick, user, 'friends')
        self._remove_from(nick, user, 'favorites')
        self._remove_from(user, nick, 'friends')
        self._remove_from(user, nick, 'favorites')

        self._send_notification(user, sc.friends_group_update, conns)

        return self._pack(sc.delete_from_friends_succ, request_id)

    def send_request(self, request_id, ip, user, msg, conns):
        """Отправить пользователю user запрос на добавление с сообщением msg
        Вызывает BadRequest, если уже отправлен запрос этому пользователю
        или отправитель пытается отправить запрос на добавление себе
        или тому, в чьем черном списке или друзьях он находится"""
        nick = self._get_nick(ip)
        self._user_exists(user)
        if nick == user:
            raise BadRequest

        c = self.db.cursor()
        c.execute('''SELECT name FROM users
                     WHERE name = %s AND
                     (%s = ANY(friends::text[]) OR
                      %s = ANY(blacklist::text[]))''',
                  (user, nick, nick))
        if c.fetchone():
            raise BadRequest

        c.execute('''SELECT from_who FROM requests
                     WHERE from_who = %s AND to_who = %s OR
                     from_who = %s AND to_who = %s''', (user, nick,
                                                        nick, user))
        if c.fetchone():
            raise BadRequest

        with self.db:
            c.execute('''INSERT INTO requests
                         VALUES (%s, %s, %s)''', (nick, user, msg))

        self._send_notification(user, sc.friends_group_update, conns)

        c.close()
        return self._pack(sc.send_request_succ, request_id)

    def delete_profile(self, request_id, ip, conns):
        """Удалить свой профиль"""
        nick = self._get_nick(ip)
        nick_tuple = (nick,)
        c = self.db.cursor()
        c.execute('''SELECT friends::text[], dialogs::text[] FROM users
                     WHERE name = %s ''', nick_tuple)
        friends, messages = c.fetchone()

        c.execute('''DELETE FROM requests
                     WHERE from_who = %s OR to_who = %s''', nick_tuple * 2)

        self._close_session(ip)

        for i in friends:
            self._remove_from(i, nick, 'friends')
            self._remove_from(i, nick, 'favorites')

        for i in messages:
            self._delete_dialog(int(i), nick)

        c.execute('''DELETE FROM profiles
                     WHERE name = %s''', nick_tuple)
        c.execute('''DELETE FROM users
                     WHERE name = %s''', nick_tuple)

        c.execute('''SELECT name FROM users''')
        for i in c.fetchall():
            self._remove_from(i['name'], nick, 'blacklist')
            self._send_notification(i['name'], sc.friends_group_update, conns)

        self.db.commit()
        c.close()
        return self._pack(sc.delete_profile_succ, request_id)

    def logout(self, request_id, ip, conns):
        """Выйти из системы"""
        self._close_session(ip)
        c = self.db.cursor()
        c.execute('''SELECT name FROM users''')
        for i in c.fetchall():
            self._send_notification(i['name'], sc.friends_group_update, conns)

        return self._pack(sc.logout_succ, request_id)

    def create_dialog(self, request_id, ip, user):
        """Создать диалог с пользователем user
        Вызывает BadRequest, если пользователь user
        не находится в друзьях или черном списке отправителя"""
        nick = self._get_nick(ip)
        self._user_exists(user)

        c = self.db.cursor()
        c.execute('''SELECT name FROM users
                     WHERE name = %s AND (%s = ANY(friends::text[])
                     OR %s = ANY(blacklist::text[]))''',
                  (nick, user, user))
        if not c.fetchone():
            raise BadRequest

        c.execute('''SELECT dialogs::text[] FROM users
                     WHERE name = %s OR name = %s''', (nick, user))
        dlg1 = set(c.fetchone()['dialogs'])
        dlg2 = set(c.fetchone()['dialogs'])

        common_dialog = dlg1.intersection(dlg2)

        if common_dialog:
            # Если у отправителя и пользователя user есть общий диалог
            return self._pack(sc.create_dialog_succ, request_id,
                              int(common_dialog.pop()))

        d_st = str(self._next_free_dialog())
        with self.db:
            c.execute('''CREATE TABLE d{} (content text,
                                           timestamp bigint,
                                           sender text)'''.format(d_st))

        self._add_to(nick, d_st, 'dialogs')
        self._add_to(user, d_st, 'dialogs')

        c.close()
        return self._pack(sc.create_dialog_succ, request_id, int(d_st))

    def profile_info(self, request_id, ip, user):
        """Получить информацию о пользователе user
        Вызывает BadRequest, если отправитель находится
        в черном списке пользователя user"""
        nick = self._get_nick(ip)
        self._user_exists(user)
        if self._is_blacklisted(nick, user):
            raise BadRequest

        c = self.db.cursor()
        c.execute('''SELECT status, email, birthday, about, image FROM profiles
                     WHERE name = %s''', (user,))

        *info, img_data = tuple(c.fetchone())

        c.close()
        return self._pack(sc.profile_info, request_id, *info,
                          b64encode(bytes(img_data)).decode())

    def remove_from_blacklist(self, request_id, ip, user, conns):
        """Удалить пользователя user из черного списка отправителя"""
        nick = self._get_nick(ip)
        self._user_exists(user)
        self._remove_from(nick, user, 'blacklist')

        self._send_notification(user, sc.friends_group_update, conns)
        return self._pack(sc.remove_from_blacklist_succ, request_id)

    def take_request_back(self, request_id, ip, user, conns):
        """Отменить запрос от отправителя к пользователю user"""
        nick = self._get_nick(ip)
        self._user_exists(user)
        self._remove_add_request(nick, user)

        self._send_notification(user, sc.friends_group_update, conns)
        return self._pack(sc.take_request_back_succ, request_id)

    def confirm_add_request(self, request_id, ip, user, conns):
        """Принять запрос на добавление от пользователя user отправителем
        Вызывает BadRequest, если пользователь user
        находится в черном списке отправителя"""
        nick = self._get_nick(ip)
        self._user_exists(user)
        if self._is_blacklisted(user, nick):
            raise BadRequest
        self._remove_add_request(user, nick)
        self._add_to(user, nick, 'friends')
        self._add_to(nick, user, 'friends')

        self._send_notification(user, sc.friends_group_update, conns)
        return self._pack(sc.confirm_add_request_succ, request_id)

    def add_to_favorites(self, request_id, ip, user):
        """Добавить пользователя user в избранное отправителя
        Вызывает BadRequest, если пользователь user
        не находится в друзьях отправителя"""
        nick = self._get_nick(ip)
        self._user_exists(user)

        c = self.db.cursor()
        c.execute('''SELECT name FROM users
                     WHERE name = %s AND %s = ANY(friends::text[])''',
                  (nick, user))
        if not c.fetchone():
            raise BadRequest
        self._add_to(nick, user, 'favorites')

        c.close()
        return self._pack(sc.add_to_favorites_succ, request_id)

    def search_msg(self, request_id, ip, dialog, text, lower_tm, upper_tm):
        """Найти в диалоге под номером dialog сообщение,
        содержащее строку text и отправленное между
        временами lower_tm и upper_tm
        Вызывает BadRequest, если lower_tm > upper_tm"""
        if lower_tm > upper_tm:
            raise BadRequest
        nick = self._get_nick(ip)
        self._user_in_dialog(nick, dialog)

        c = self.db.cursor()
        c.execute('''SELECT * FROM d{}
                     WHERE POSITION(%s IN content) > 0 AND
                     timestamp BETWEEN %s AND %s'''.format(dialog),
                  (text, lower_tm, upper_tm))
        result = map(tuple, c.fetchall())

        c.close()
        return self._pack(sc.search_msg_result, request_id, list(result))

    def remove_from_favorites(self, request_id, ip, user):
        """Удалить пользователя user из избранного отправителя"""
        nick = self._get_nick(ip)
        self._user_exists(user)
        self._remove_from(nick, user, 'favorites')
        return self._pack(sc.remove_from_favorites_succ, request_id)

    def add_requests(self, request_id, ip):
        """Получить запросы на добавление к отправителю и от него"""
        nick = self._get_nick(ip)
        c = self.db.cursor()
        c.execute('''SELECT name FROM sessions''')
        online = {i['name'] for i in c.fetchall()}

        c.execute('''SELECT from_who, message FROM requests
                     WHERE to_who = %s''', (nick,))
        inc = [(*i, i[0] in online) for i in map(tuple, c.fetchall())]
        c.execute('''SELECT to_who, message FROM requests
                     WHERE from_who = %s''', (nick,))
        outc = [(*i, i[0] in online) for i in map(tuple, c.fetchall())]
        c.close()
        return self._pack(sc.add_requests, request_id, [inc, outc])

    def decline_add_request(self, request_id, ip, user, conns):
        """Отменить запрос на добавление от пользователя user к отправителю"""
        nick = self._get_nick(ip)
        self._user_exists(user)
        self._remove_add_request(user, nick)

        self._send_notification(user, sc.friends_group_update, conns)
        return self._pack(sc.decline_add_request_succ, request_id)

    def set_image(self, request_id, ip, img_data):
        """Установить в качестве изображения пользователя картинку,
        бинарные данные в base64 которой находятся в img_data"""
        nick = self._get_nick(ip)
        c = self.db.cursor()
        with self.db:
            c.execute('''UPDATE profiles SET image = %s
                         WHERE name = %s''',
                      (b64decode(img_data), nick))
        c.close()
        return self._pack(sc.set_image_succ, request_id)