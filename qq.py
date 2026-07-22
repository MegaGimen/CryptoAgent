import os
import re
import asyncio
import botpy
from botpy import logging
from botpy.message import C2CMessage
from flask import Flask, request, jsonify
from threading import Thread

from env_config import load_project_env
load_project_env()

# 设置日志级别
_log = logging.get_logger()

def update_env_variable(key, value):
    """
    自动更新 .env 文件中的环境变量
    """
    from env_config import ENV_FILE
    env_path = str(ENV_FILE)
    if not os.path.exists(env_path):
        _log.error(f"未找到 .env 文件: {env_path}")
        return False
    
    with open(env_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    pattern = re.compile(rf'^{key}=.*$', re.MULTILINE)
    new_line = f'{key}="{value}"'
    
    if pattern.search(content):
        new_content = pattern.sub(new_line, content)
    else:
        new_content = content.strip() + f"\n{new_line}\n"
    
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    
    _log.info(f"已自动更新 .env 中的 {key}")
    return True

class MyClient(botpy.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_openid = os.getenv("QQ_BOT_USER_ID")

    async def on_ready(self):
        """
        机器人准备就绪后的回调
        """
        _log.info(f"机器人 [{self.robot.name}] (ID: {self.robot.id}) 已上线！")
        self.user_openid = os.getenv("QQ_BOT_USER_ID")
        
        if self.user_openid and self.user_openid != "YOUR_USER_OPENID":
            try:
                _log.info(f"正在尝试向用户 {self.user_openid} 推送启动消息...")
                await self.api.post_c2c_message(openid=self.user_openid, content="你好，机器人已成功启动并开启了 HTTP 监听 (9182)！")
                _log.info("主动推送成功！")
            except Exception as e:
                _log.error(f"推送消息失败: {e}")
        else:
            _log.warning("未配置有效的 QQ_BOT_USER_ID，无法发送推送消息。")

    async def on_c2c_message_create(self, message: C2CMessage):
        """
        处理 C2C 私聊消息，并实现 OpenID 自动更新
        """
        author = getattr(message, "author", {})
        user_id = None
        
        if isinstance(author, dict):
            user_id = author.get("user_openid") or author.get("id")
        else:
            user_id = getattr(author, "user_openid", None) or getattr(author, "id", None)

        if user_id:
            if user_id != self.user_openid:
                _log.info(f"检测到新的 OpenID: {user_id}，正在更新配置...")
                if update_env_variable("QQ_BOT_USER_ID", user_id):
                    os.environ["QQ_BOT_USER_ID"] = user_id
                    self.user_openid = user_id
            
            _log.info(f"收到来自 {user_id} 的消息: {message.content}")
            try:
                await self.api.post_c2c_message(openid=user_id, content=f"你好！我已经记录了你的 OpenID。\n当前 ID：{user_id}\n你现在可以通过 POST 请求到 :9182/push 给我也发消息了。")
            except Exception as e:
                _log.error(f"回复失败: {e}")

    async def push_message(self, content):
        """
        供外部调用的推送接口
        """
        if not self.user_openid or self.user_openid == "YOUR_USER_OPENID":
            return False, "未配置有效的 QQ_BOT_USER_ID"
        
        try:
            await self.api.post_c2c_message(openid=self.user_openid, content=content)
            _log.info(f"成功推送消息: {content}")
            return True, "推送成功"
        except Exception as e:
            _log.error(f"推送消息失败: {e}")
            return False, str(e)

# --- Flask Web 服务 ---
app = Flask(__name__)
bot_client = None
loop = None

@app.route('/push', methods=['POST'])
def handle_push():
    data = request.json
    if not data or 'msg' not in data:
        return jsonify({"code": 400, "msg": "缺少 msg 字段"}), 400
    
    msg_content = data['msg']
    
    # 在 asyncio 事件循环中运行推送任务
    future = asyncio.run_coroutine_threadsafe(bot_client.push_message(msg_content), loop)
    success, error_msg = future.result()
    
    if success:
        return jsonify({"code": 200, "msg": "推送成功"}), 200
    else:
        return jsonify({"code": 500, "msg": f"推送失败: {error_msg}"}), 500

def run_flask():
    app.run(host='0.0.0.0', port=9182)

if __name__ == "__main__":
    appid = os.getenv("QQ_BOT_APPID")
    secret = os.getenv("QQ_BOT_SECRET")

    if not appid or not secret or appid == "YOUR_APPID":
        _log.error("请先在 .env 文件中配置 QQ_BOT_APPID 和 QQ_BOT_SECRET")
    else:
        intents = botpy.Intents(public_messages=True)
        bot_client = MyClient(intents=intents)
        
        # 获取当前线程的事件循环，供 Flask 线程使用
        loop = asyncio.get_event_loop()
        
        # 启动 Flask 线程
        flask_thread = Thread(target=run_flask, daemon=True)
        flask_thread.start()
        
        # 运行机器人
        bot_client.run(appid=appid, secret=secret)
