"""TUI messages, selected from the process locale without requiring installed locales."""

import os


def locale_language(environ=None):
    """Use POSIX message-locale precedence; an unsupported locale uses English."""
    env = os.environ if environ is None else environ
    locale = next((env.get(key) for key in ('LC_ALL', 'LC_MESSAGES', 'LANG') if env.get(key)), 'C')
    name = locale.split('.', 1)[0].split('@', 1)[0].replace('-', '_').lower()
    parts = name.split('_')
    if parts[0] == 'zh':
        return (
            'zh_Hant'
            if any(part in {'hant', 'tw', 'hk', 'mo'} for part in parts[1:])
            else 'zh_Hans'
        )
    return 'en'


MESSAGES = {
    'zh_Hans': {
        '(not configured)': '（未配置）',
        '(invalid address)': '（无效地址）',
        'Identity or service changed; choose a view again.': '身份或服务已变化；请重新选择视图。',
        'Read failed: ': '读取失败：',
        'Use retry to read again; no ACK or other writes are sent automatically.': 'retry 重新读取；不自动提交 ACK 或其他写入。',
        '(empty)': '（空）',
        'n next page': 'n 下一页',
        'Not signed in (public content)': '未登录（公开内容）',
        'Identity: ': '身份：',
        'Not signed in; public content is available': '未登录；可以浏览公开内容',
        'Service: ': '服务：',
        'Use msg identity to manage your identity; the TUI does not accept keys or tokens.': '身份管理请使用 msg identity；TUI 不接收密钥或令牌。',
        ' requires a signed-in identity.': ' 需要已登录身份。',
        'Files requires a signed-in identity.': 'Files 需要已登录身份。',
        'Local certificate IDs (validity is checked on every request):': '本地证书 ID（在线有效性由每次请求重新检查）：',
        'Usage: s <scope> <terms>': '用法：s <scope> <terms>',
        'Search': '搜索',
        'Usage: t <resource-id>': '用法：t <resource-id>',
        'Thread ': '线程 ',
        'Usage: r <resource-id>': '用法：r <resource-id>',
        'Status: ': '状态：',
        '(no text body)': '（无文本正文）',
        'Reading does not send ACK.': '读取不会发送 ACK。',
        'No next page.': '没有下一页。',
        'No request to retry.': '没有可重新读取的请求。',
        'This item has no readable identifier.': '该条目没有可读取的标识。',
        'Number is not on this page.': '序号不在本页。',
        'Home': '首页',
        'Inbox': '收件箱',
        'Topics': '话题',
        'Groups': '组织',
        'Outbox': '发件箱',
        'Following': '关注',
        'Notes': '笔记',
        'Todos': '待办',
        'Files': '文件',
        'Commands: h home, id identity, topics, following, i inbox, o outbox, notes, todos, files, groups, credentials local credentials, s <scope> <terms> search, t <id> thread, r <id> read, n next page, retry, q quit.': '命令：h 首页，id 身份，topics 话题，following 关注，i 收件箱，o 发件箱，notes 笔记，todos 待办，files 文件，groups 组织，credentials 本地凭据，s <scope> <terms> 搜索，t <id> 线程，r <id> 读取，n 下一页，retry 重新读取，q 退出。',
    },
    'zh_Hant': {
        '(not configured)': '（未設定）',
        '(invalid address)': '（無效位址）',
        'Identity or service changed; choose a view again.': '身分或服務已變更；請重新選擇檢視。',
        'Read failed: ': '讀取失敗：',
        'Use retry to read again; no ACK or other writes are sent automatically.': 'retry 重新讀取；不自動提交 ACK 或其他寫入。',
        '(empty)': '（空）',
        'n next page': 'n 下一頁',
        'Not signed in (public content)': '未登入（公開內容）',
        'Identity: ': '身分：',
        'Not signed in; public content is available': '未登入；可以瀏覽公開內容',
        'Service: ': '服務：',
        'Use msg identity to manage your identity; the TUI does not accept keys or tokens.': '身分管理請使用 msg identity；TUI 不接收金鑰或權杖。',
        ' requires a signed-in identity.': ' 需要已登入身分。',
        'Files requires a signed-in identity.': '檔案需要已登入身分。',
        'Local certificate IDs (validity is checked on every request):': '本機憑證 ID（有效性由每次請求重新檢查）：',
        'Usage: s <scope> <terms>': '用法：s <scope> <terms>',
        'Search': '搜尋',
        'Usage: t <resource-id>': '用法：t <resource-id>',
        'Thread ': '討論串 ',
        'Usage: r <resource-id>': '用法：r <resource-id>',
        'Status: ': '狀態：',
        '(no text body)': '（無文字內文）',
        'Reading does not send ACK.': '讀取不會傳送 ACK。',
        'No next page.': '沒有下一頁。',
        'No request to retry.': '沒有可重新讀取的請求。',
        'This item has no readable identifier.': '此項目沒有可讀取的識別碼。',
        'Number is not on this page.': '序號不在本頁。',
        'Home': '首頁',
        'Inbox': '收件匣',
        'Topics': '話題',
        'Groups': '組織',
        'Outbox': '寄件匣',
        'Following': '追蹤',
        'Notes': '筆記',
        'Todos': '待辦',
        'Files': '檔案',
        'Commands: h home, id identity, topics, following, i inbox, o outbox, notes, todos, files, groups, credentials local credentials, s <scope> <terms> search, t <id> thread, r <id> read, n next page, retry, q quit.': '命令：h 首頁，id 身分，topics 話題，following 追蹤，i 收件匣，o 寄件匣，notes 筆記，todos 待辦，files 檔案，groups 組織，credentials 本機憑證，s <scope> <terms> 搜尋，t <id> 討論串，r <id> 讀取，n 下一頁，retry 重新讀取，q 離開。',
    },
}


class Translator:
    def __init__(self, language=None):
        self.language = language or locale_language()
        self.messages = MESSAGES.get(self.language, {})

    def __call__(self, message):
        return self.messages.get(message, message)
