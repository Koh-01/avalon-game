#!/usr/bin/env python3
"""
阿瓦隆 (Avalon) 网络游戏服务器
WebSocket + HTTP 实现的多人在线桌游
"""

import asyncio
import json
import random
import uuid
import traceback
from dataclasses import dataclass, field, asdict
from typing import Optional
from aiohttp import web
import aiohttp

# ─────────────────────────────────────────
#  数据模型
# ─────────────────────────────────────────

ROLES = {
    "merlin":      {"team": "good", "name": "梅林",      "emoji": "👑"},
    "percival":    {"team": "good", "name": "派西维尔",  "emoji": "🛡️"},
    "loyal":       {"team": "good", "name": "亚瑟忠臣",  "emoji": "⚔️"},
    "mordred":     {"team": "evil", "name": "莫德雷德",  "emoji": "🗡️"},
    "morgana":     {"team": "evil", "name": "莫甘娜",    "emoji": "🔮"},
    "assassin":    {"team": "evil", "name": "刺客",      "emoji": "🎯"},
    "minion":      {"team": "evil", "name": "莫德雷德爪牙", "emoji": "💀"},
}

# 每轮任务所需人数 [5人, 6人, 7人, 8人, 9人, 10人]
QUEST_SIZES = {
    5:  [2, 3, 2, 3, 3],
    6:  [2, 3, 4, 3, 4],
    7:  [2, 3, 3, 4, 4],
    8:  [3, 4, 4, 5, 5],
    9:  [3, 4, 4, 5, 5],
    10: [3, 4, 4, 5, 5],
}

# 角色配置（按人数）
ROLE_CONFIGS = {
    5:  ["merlin", "percival", "loyal", "morgana", "assassin"],
    6:  ["merlin", "percival", "loyal", "loyal", "morgana", "assassin"],
    7:  ["merlin", "percival", "loyal", "loyal", "mordred", "morgana", "assassin"],
    8:  ["merlin", "percival", "loyal", "loyal", "loyal", "mordred", "morgana", "assassin"],
    9:  ["merlin", "percival", "loyal", "loyal", "loyal", "loyal", "mordred", "morgana", "assassin"],
    10: ["merlin", "percival", "loyal", "loyal", "loyal", "loyal", "mordred", "morgana", "assassin", "minion"],
}

@dataclass
class Player:
    id: str
    name: str
    role: Optional[str] = None
    ws: object = field(default=None, repr=False, compare=False)
    online: bool = True

@dataclass
class Game:
    room_id: str
    players: list = field(default_factory=list)
    host_id: str = ""
    phase: str = "lobby"          # lobby / night / quest / vote / execute / assassinate / end
    round: int = 0                # 0-4
    leader_idx: int = 0
    team: list = field(default_factory=list)         # 当前队伍玩家id
    vote_reject_count: int = 0    # 连续拒绝次数
    quest_results: list = field(default_factory=list)  # True=成功, False=失败
    votes: dict = field(default_factory=dict)        # player_id -> True/False
    mission_cards: dict = field(default_factory=dict) # player_id -> True/False
    winner: Optional[str] = None
    assassin_target: Optional[str] = None
    night_ack: set = field(default_factory=set)      # 已确认夜晚信息的玩家

    def good_wins(self): return self.quest_results.count(True) >= 3
    def evil_wins(self): return self.quest_results.count(False) >= 3

    def quest_size(self):
        n = len(self.players)
        sizes = QUEST_SIZES.get(n, QUEST_SIZES[7])
        return sizes[self.round]

    def leader(self):
        return self.players[self.leader_idx % len(self.players)]

    def get_player(self, pid):
        return next((p for p in self.players if p.id == pid), None)

    def evil_players(self):
        return [p for p in self.players if p.role and ROLES[p.role]["team"] == "evil"]

    def good_players(self):
        return [p for p in self.players if p.role and ROLES[p.role]["team"] == "good"]

rooms: dict[str, Game] = {}

# ─────────────────────────────────────────
#  工具函数
# ─────────────────────────────────────────

def make_public_state(game: Game, viewer_id: str):
    """生成发给特定玩家的游戏状态（含私密信息）"""
    vp = game.get_player(viewer_id)
    viewer_role = vp.role if vp else None
    viewer_team = ROLES[viewer_role]["team"] if viewer_role else None

    players_info = []
    for p in game.players:
        info = {
            "id": p.id,
            "name": p.name,
            "online": p.online,
            "is_leader": (p == game.leader() and game.phase not in ("lobby","night","end")),
            "in_team": p.id in game.team,
        }
        # 坏人互相看角色，好人只看自己
        if viewer_role and p.id == viewer_id:
            info["role"] = p.role
            info["role_name"] = ROLES[p.role]["name"]
            info["role_emoji"] = ROLES[p.role]["emoji"]
            info["team"] = ROLES[p.role]["team"]
        elif viewer_team == "evil" and p.role and ROLES[p.role]["team"] == "evil":
            info["role"] = p.role
            info["role_name"] = ROLES[p.role]["name"]
            info["role_emoji"] = ROLES[p.role]["emoji"]
            info["team"] = "evil"
        players_info.append(info)

    # 梅林看到的坏人（除莫德雷德）
    visible_evil_to_merlin = []
    if viewer_role == "merlin":
        for p in game.players:
            if p.role and ROLES[p.role]["team"] == "evil" and p.role != "mordred":
                visible_evil_to_merlin.append(p.id)

    # 派西维尔看到的梅林和莫甘娜（乱序）
    visible_to_percival = []
    if viewer_role == "percival":
        pool = [p for p in game.players if p.role in ("merlin", "morgana")]
        random.shuffle(pool)
        visible_to_percival = [p.id for p in pool]

    state = {
        "room_id": game.room_id,
        "phase": game.phase,
        "round": game.round,
        "quest_results": game.quest_results,
        "quest_size": game.quest_size() if game.phase != "end" else 0,
        "team": game.team,
        "vote_reject_count": game.vote_reject_count,
        "leader_id": game.leader().id if game.players else "",
        "players": players_info,
        "winner": game.winner,
        "assassin_target": game.assassin_target,
        "host_id": game.host_id,
        "my_id": viewer_id,
        "my_role": viewer_role,
        "my_team": viewer_team,
        "visible_evil_to_merlin": visible_evil_to_merlin,
        "visible_to_percival": visible_to_percival,
        "voted": viewer_id in game.votes,
        "mission_played": viewer_id in game.mission_cards,
        "vote_tally": len(game.votes),
        "mission_tally": len(game.mission_cards),
        "total_players": len(game.players),
    }
    return state

async def broadcast(game: Game, msg: dict, exclude=None):
    """向房间内所有在线玩家广播（各自看到自己的私密版本）"""
    for p in game.players:
        if p.ws and p.online and p.id != exclude:
            try:
                personal = dict(msg)
                if msg.get("type") == "state":
                    personal["data"] = make_public_state(game, p.id)
                await p.ws.send_json(personal)
            except Exception as e:
                print(f"Broadcast error to {p.name}: {e}")
                traceback.print_exc()

async def send_state(game: Game, player: Player):
    if player.ws and player.online:
        try:
            await player.ws.send_json({
                "type": "state",
                "data": make_public_state(game, player.id)
            })
        except Exception as e:
            print(f"Send state error to {player.name}: {e}")
            traceback.print_exc()

async def notify(game: Game, message: str, color="white"):
    await broadcast(game, {"type": "notify", "message": message, "color": color})

# ─────────────────────────────────────────
#  游戏逻辑
# ─────────────────────────────────────────

async def start_game(game: Game):
    n = len(game.players)
    if n < 5 or n > 10:
        return False
    roles = ROLE_CONFIGS[n][:]
    random.shuffle(roles)
    random.shuffle(game.players)
    for i, p in enumerate(game.players):
        p.role = roles[i]
    game.round = 0
    game.leader_idx = random.randint(0, n - 1)
    game.quest_results = []
    game.vote_reject_count = 0
    game.team = []
    game.votes = {}
    game.mission_cards = {}
    game.winner = None
    game.phase = "night"
    game.night_ack = set()
    await broadcast(game, {"type": "state"})
    await notify(game, "🌙 夜晚降临，请查看您的角色信息...", "purple")
    return True

async def process_vote(game: Game):
    """处理队伍投票结果"""
    approve = sum(1 for v in game.votes.values() if v)
    reject = len(game.votes) - approve
    if approve > reject:
        game.phase = "execute"
        game.mission_cards = {}
        await notify(game, f"✅ 投票通过！({approve}赞成/{reject}反对) 队员开始执行任务...", "green")
        game.vote_reject_count = 0
    else:
        game.vote_reject_count += 1
        game.votes = {}
        game.team = []
        if game.vote_reject_count >= 3:
            # 第3次强制通过，无需投票
            game.leader_idx = (game.leader_idx + 1) % len(game.players)
            game.phase = "quest"
            await notify(game, f"❌ 再次拒绝！({approve}赞成/{reject}反对) 连续3次拒绝，下一位领袖强制执行！", "red")
        else:
            game.leader_idx = (game.leader_idx + 1) % len(game.players)
            game.phase = "quest"
            await notify(game, f"❌ 投票未通过！({approve}赞成/{reject}反对) 换下一位领袖...", "orange")
    await broadcast(game, {"type": "state"})

async def process_mission(game: Game):
    """处理任务结果"""
    cards = list(game.mission_cards.values())
    random.shuffle(cards)
    fail_count = cards.count(False)
    n = len(game.players)
    # 7人以上第4轮需要2张失败
    need_fails = 2 if (n >= 7 and game.round == 3) else 1
    success = fail_count < need_fails

    game.quest_results.append(success)
    result_str = "✅ 任务成功！" if success else f"❌ 任务失败！(有{fail_count}张失败牌)"
    color = "green" if success else "red"
    await notify(game, f"第{game.round+1}轮任务：{result_str}", color)
    await broadcast(game, {
        "type": "mission_result",
        "cards": cards,
        "success": success,
        "round": game.round
    })

    if game.good_wins():
        game.phase = "assassinate"
        await notify(game, "🏆 好人赢得了三次任务！但刺客还有最后的机会...", "gold")
    elif game.evil_wins():
        game.phase = "end"
        game.winner = "evil"
        # 揭示所有角色
        await notify(game, "💀 坏人阻止了三次任务，邪恶阵营获胜！", "red")
    else:
        game.round += 1
        game.leader_idx = (game.leader_idx + 1) % len(game.players)
        game.phase = "quest"
        game.team = []
        game.votes = {}
        game.mission_cards = {}
        await notify(game, f"开始第{game.round+1}轮任务...", "blue")

    await broadcast(game, {"type": "state"})

# ─────────────────────────────────────────
#  WebSocket 消息处理
# ─────────────────────────────────────────

async def handle_message(ws, game: Game, player: Player, msg: dict):
    action = msg.get("action")

    if action == "night_ack":
        game.night_ack.add(player.id)
        if len(game.night_ack) >= len(game.players):
            game.phase = "quest"
            await broadcast(game, {"type": "state"})
            await notify(game, f"☀️ 黎明降临！领袖 {game.leader().name} 请组建队伍（需{game.quest_size()}人）", "blue")
        else:
            await send_state(game, player)

    elif action == "select_team":
        if player.id != game.leader().id or game.phase != "quest":
            return
        team = msg.get("team", [])
        size = game.quest_size()
        if len(team) != size:
            await ws.send_json({"type": "error", "message": f"需要选择{size}名队员"})
            return
        # 验证所有id有效
        valid_ids = {p.id for p in game.players}
        if not all(t in valid_ids for t in team):
            return
        game.team = team
        game.votes = {}
        game.phase = "vote"
        leader = game.leader()
        await notify(game, f"👑 {leader.name} 提名了队伍，全员开始投票...", "blue")
        await broadcast(game, {"type": "state"})

    elif action == "vote":
        if game.phase != "vote" or player.id in game.votes:
            return
        game.votes[player.id] = msg.get("approve", True)
        await broadcast(game, {"type": "vote_progress", "count": len(game.votes), "total": len(game.players)})
        if len(game.votes) >= len(game.players):
            await process_vote(game)

    elif action == "mission_card":
        if game.phase != "execute" or player.id not in game.team or player.id in game.mission_cards:
            return
        card = msg.get("success", True)
        # 好人只能出成功
        if ROLES[player.role]["team"] == "good":
            card = True
        game.mission_cards[player.id] = card
        await broadcast(game, {"type": "mission_progress", "count": len(game.mission_cards), "total": len(game.team)})
        if len(game.mission_cards) >= len(game.team):
            await process_mission(game)

    elif action == "assassinate":
        evil_ids = {p.id for p in game.evil_players()}
        if player.id not in evil_ids or game.phase != "assassinate":
            return
        target_id = msg.get("target")
        target = game.get_player(target_id)
        if not target:
            return
        game.assassin_target = target_id
        if target.role == "merlin":
            game.winner = "evil"
            await notify(game, f"🎯 刺客刺杀了 {target.name}（梅林）！邪恶阵营逆转获胜！", "red")
        else:
            game.winner = "good"
            await notify(game, f"🎯 刺客刺杀了 {target.name}，但TA不是梅林！正义阵营获胜！", "gold")
        game.phase = "end"
        await broadcast(game, {"type": "state"})

    elif action == "restart":
        if player.id != game.host_id:
            return
        await start_game(game)

    elif action == "force_reset":
        if player.id != game.host_id:
            await ws.send_json({"type": "error", "message": "只有房主才能强制重置"})
            return
        # 重置游戏状态，保留玩家
        game.phase = "lobby"
        game.round = 0
        game.leader_idx = 0
        game.team = []
        game.votes = {}
        game.mission_cards = {}
        game.quest_results = []
        game.vote_reject_count = 0
        game.winner = None
        game.assassin_target = None
        game.night_ack = set()
        for p in game.players:
            p.role = None
        await broadcast(game, {"type": "state"})
        await notify(game, f"🔄 房主 {player.name} 强制重置了游戏，回到大厅", "orange")

# ─────────────────────────────────────────
#  HTTP & WebSocket 路由
# ─────────────────────────────────────────

async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    room_id = request.match_info.get("room_id", "").upper()
    player_id = request.rel_url.query.get("player_id", str(uuid.uuid4()))
    player_name = request.rel_url.query.get("name", "匿名玩家")[:12]

    if room_id not in rooms:
        await ws.send_json({"type": "error", "message": "房间不存在"})
        await ws.close()
        return ws

    game = rooms[room_id]

    # 重连或新加入
    existing = game.get_player(player_id)
    if existing:
        player = existing
        player.ws = ws
        player.online = True
        await notify(game, f"🔗 {player.name} 重新连线了", "cyan")
    else:
        if game.phase != "lobby":
            await ws.send_json({"type": "error", "message": "游戏已开始，无法加入"})
            await ws.close()
            return ws
        if len(game.players) >= 10:
            await ws.send_json({"type": "error", "message": "房间已满"})
            await ws.close()
            return ws
        player = Player(id=player_id, name=player_name, ws=ws)
        game.players.append(player)
        if not game.host_id:
            game.host_id = player_id
        await notify(game, f"🎉 {player.name} 加入了游戏", "green")

    await send_state(game, player)

    async for raw in ws:
        if raw.type == aiohttp.WSMsgType.TEXT:
            try:
                msg = json.loads(raw.data)
                if msg.get("action") == "start_game":
                    if player.id == game.host_id and game.phase == "lobby" and len(game.players) >= 5:
                        await start_game(game)
                    else:
                        await ws.send_json({"type": "error", "message": "无法开始游戏（需要至少5名玩家且您是房主）"})
                else:
                    await handle_message(ws, game, player, msg)
            except Exception as e:
                print(f"WebSocket Message Error: {e}")
                traceback.print_exc()
        elif raw.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
            break

    player.online = False
    player.ws = None
    await notify(game, f"📴 {player.name} 断线了", "gray")
    return ws

async def health(request):
    return web.json_response({"status": "ok", "rooms": len(rooms)})

async def create_room(request):
    room_id = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=5))
    while room_id in rooms:
        room_id = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=5))
    rooms[room_id] = Game(room_id=room_id)
    return web.json_response({"room_id": room_id})

async def index(request):
    raise web.HTTPFound('/static/index.html')

def build_app():
    import os
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    static_path = os.path.join(BASE_DIR, 'static')
    os.makedirs(static_path, exist_ok=True)

    @web.middleware
    async def cors_middleware(request, handler):
        # 修复：预检请求处理
        if request.method == 'OPTIONS':
            resp = web.Response()
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return resp
            
        try:
            resp = await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:
            raise
            
        # 修复：如果是 WebSocket，不要去修改 Headers
        if not isinstance(resp, web.WebSocketResponse):
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get('/', index)
    app.router.add_get('/health', health)
    app.router.add_post('/api/room', create_room)
    app.router.add_get('/ws/{room_id}', ws_handler)
    app.router.add_static('/static', path=static_path, name='static')
    return app

if __name__ == '__main__':
    import os
    port = int(os.environ.get("PORT", 8080))
    print(f"🏰 阿瓦隆游戏服务器启动于 http://0.0.0.0:{port}")
    web.run_app(build_app(), host='0.0.0.0', port=port)
