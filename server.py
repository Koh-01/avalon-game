#!/usr/bin/env python3
"""
阿瓦隆 (Avalon) 网络游戏服务器 - HTTP 长轮询版
彻底移除 WebSocket，完美适配 Render 免费层
"""

import asyncio
import json
import random
import uuid
import traceback
import os
from dataclasses import dataclass, field
from typing import Optional
from aiohttp import web

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

QUEST_SIZES = {
    5:  [2, 3, 2, 3, 3], 6:  [2, 3, 4, 3, 4], 7:  [2, 3, 3, 4, 4],
    8:  [3, 4, 4, 5, 5], 9:  [3, 4, 4, 5, 5], 10: [3, 4, 4, 5, 5],
}

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
    online: bool = True

@dataclass
class Game:
    room_id: str
    version: int = 0
    notifications: list = field(default_factory=list)
    players: list = field(default_factory=list)
    host_id: str = ""
    phase: str = "lobby"
    round: int = 0
    leader_idx: int = 0
    team: list = field(default_factory=list)
    vote_reject_count: int = 0
    quest_results: list = field(default_factory=list)
    votes: dict = field(default_factory=dict)
    mission_cards: dict = field(default_factory=dict)
    winner: Optional[str] = None
    assassin_target: Optional[str] = None
    night_ack: set = field(default_factory=set)
    # 持久化记录上一轮的结果
    last_vote_summary: str = ""
    last_mission_summary: str = ""

    def good_wins(self): return self.quest_results.count(True) >= 3
    def evil_wins(self): return self.quest_results.count(False) >= 3
    def quest_size(self): return QUEST_SIZES.get(len(self.players), QUEST_SIZES[7])[self.round]
    def leader(self): return self.players[self.leader_idx % len(self.players)]
    def get_player(self, pid): return next((p for p in self.players if p.id == pid), None)
    def evil_players(self): return [p for p in self.players if p.role and ROLES[p.role]["team"] == "evil"]
    
    def touch(self):
        self.version += 1

    def notify(self, msg: str, color: str = "white"):
        self.notifications.append({"msg": msg, "color": color})
        self.touch()

rooms: dict[str, Game] = {}

# ─────────────────────────────────────────
#  工具函数
# ─────────────────────────────────────────

def make_public_state(game: Game, viewer_id: str):
    vp = game.get_player(viewer_id)
    viewer_role = vp.role if vp else None
    viewer_team = ROLES[viewer_role]["team"] if viewer_role else None

    players_info = []
    for p in game.players:
        info = {
            "id": p.id, "name": p.name, "online": p.online,
            "is_leader": (p == game.leader() and game.phase not in ("lobby","night","end")),
            "in_team": p.id in game.team,
        }
        if viewer_role and p.id == viewer_id:
            info.update({"role": p.role, "role_name": ROLES[p.role]["name"], "role_emoji": ROLES[p.role]["emoji"], "team": ROLES[p.role]["team"]})
        elif viewer_team == "evil" and p.role and ROLES[p.role]["team"] == "evil":
            info.update({"role": p.role, "role_name": ROLES[p.role]["name"], "role_emoji": ROLES[p.role]["emoji"], "team": "evil"})
        players_info.append(info)

    visible_evil = [p.id for p in game.players if p.role and ROLES[p.role]["team"] == "evil" and p.role != "mordred"] if viewer_role == "merlin" else []
    
    visible_percival = []
    if viewer_role == "percival":
        pool = [p for p in game.players if p.role in ("merlin", "morgana")]
        random.shuffle(pool)
        visible_percival = [p.id for p in pool]

    return {
        "room_id": game.room_id, "phase": game.phase, "round": game.round,
        "quest_results": game.quest_results, "quest_size": game.quest_size() if game.phase != "end" else 0,
        "team": game.team, "vote_reject_count": game.vote_reject_count,
        "leader_id": game.leader().id if game.players else "", "players": players_info,
        "winner": game.winner, "assassin_target": game.assassin_target, "host_id": game.host_id,
        "my_id": viewer_id, "my_role": viewer_role, "my_team": viewer_team,
        "visible_evil_to_merlin": visible_evil, "visible_to_percival": visible_percival,
        "voted": viewer_id in game.votes, "mission_played": viewer_id in game.mission_cards,
        "vote_tally": len(game.votes), "mission_tally": len(game.mission_cards), "total_players": len(game.players),
        "last_vote_summary": game.last_vote_summary,
        "last_mission_summary": game.last_mission_summary
    }

# ─────────────────────────────────────────
#  游戏逻辑处理
# ─────────────────────────────────────────

def process_vote(game: Game):
    approve = sum(1 for v in game.votes.values() if v)
    reject = len(game.votes) - approve
    game.last_vote_summary = f"{approve} 赞成 / {reject} 反对"
    
    if approve > reject:
        game.phase = "execute"
        game.mission_cards = {}
        game.notify(f"✅ 投票通过！({game.last_vote_summary}) 队员开始执行任务...", "green")
        game.vote_reject_count = 0
    else:
        game.vote_reject_count += 1
        game.votes = {}
        game.team = []
        if game.vote_reject_count >= 3:
            game.leader_idx = (game.leader_idx + 1) % len(game.players)
            game.phase = "quest"
            game.notify(f"❌ 连续3次拒绝！({game.last_vote_summary}) 下一位领袖强制执行！", "red")
        else:
            game.leader_idx = (game.leader_idx + 1) % len(game.players)
            game.phase = "quest"
            game.notify(f"❌ 投票未通过！({game.last_vote_summary}) 换下一位领袖...", "orange")

def process_mission(game: Game):
    cards = list(game.mission_cards.values())
    random.shuffle(cards)
    fail_count = cards.count(False)
    success_count = cards.count(True)
    need_fails = 2 if (len(game.players) >= 7 and game.round == 3) else 1
    success = fail_count < need_fails

    game.quest_results.append(success)
    game.last_mission_summary = f"{success_count} 成功 / {fail_count} 失败"
    
    result_str = f"✅ 任务成功！({game.last_mission_summary})" if success else f"❌ 任务失败！({game.last_mission_summary})"
    color = "green" if success else "red"
    game.notify(f"第{game.round+1}轮任务：{result_str}", color)

    if game.good_wins():
        game.phase = "assassinate"
        game.notify("🏆 好人赢得了三次任务！但刺客还有最后的机会...", "gold")
    elif game.evil_wins():
        game.phase = "end"
        game.winner = "evil"
        game.notify("💀 坏人阻止了三次任务，邪恶阵营获胜！", "red")
    else:
        game.round += 1
        game.leader_idx = (game.leader_idx + 1) % len(game.players)
        game.phase = "quest"
        game.team, game.votes, game.mission_cards = [], {}, {}
        game.notify(f"开始第{game.round+1}轮任务...", "blue")

# ─────────────────────────────────────────
#  HTTP 路由接口
# ─────────────────────────────────────────

async def api_create_room(request):
    room_id = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=5))
    while room_id in rooms: room_id = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=5))
    rooms[room_id] = Game(room_id=room_id)
    return web.json_response({"room_id": room_id})

async def api_join(request):
    data = await request.json()
    room_id, player_id, name = data.get("room_id"), data.get("player_id"), data.get("name")
    if room_id not in rooms: return web.json_response({"error": "房间不存在"}, status=400)
    
    game = rooms[room_id]
    player = game.get_player(player_id)
    if player:
        player.online = True
        game.notify(f"🔗 {player.name} 回到了游戏", "cyan")
    else:
        if game.phase != "lobby": return web.json_response({"error": "游戏已开始，无法加入"}, status=400)
        if len(game.players) >= 10: return web.json_response({"error": "房间已满"}, status=400)
        game.players.append(Player(id=player_id, name=name))
        if not game.host_id: game.host_id = player_id
        game.notify(f"🎉 {name} 加入了游戏", "green")
    
    return web.json_response({"status": "ok"})

async def api_sync(request):
    room_id = request.rel_url.query.get("room_id")
    player_id = request.rel_url.query.get("player_id")
    client_v = int(request.rel_url.query.get("v", -1))

    game = rooms.get(room_id)
    if not game: return web.json_response({"error": "房间已解散"}, status=404)
    
    p = game.get_player(player_id)
    if p: p.online = True

    for _ in range(30):
        if game.version != client_v:
            break
        await asyncio.sleep(0.5)

    return web.json_response({
        "v": game.version,
        "notifications": game.notifications, 
        "state": make_public_state(game, player_id) if p else None
    })

async def api_action(request):
    data = await request.json()
    game = rooms.get(data.get("room_id"))
    if not game: return web.json_response({"error": "Game not found"}, status=400)
    
    player_id = data.get("player_id")
    player = game.get_player(player_id)
    if not player: return web.json_response({"error": "Player not found"}, status=400)
    
    action = data.get("action")

    if action == "start_game" and player.id == game.host_id and game.phase == "lobby":
        n = len(game.players)
        if n >= 5:
            roles = ROLE_CONFIGS[n][:]
            random.shuffle(roles)
            random.shuffle(game.players)
            for i, p in enumerate(game.players): p.role = roles[i]
            game.round, game.vote_reject_count, game.phase = 0, 0, "night"
            game.leader_idx = random.randint(0, n - 1)
            game.team, game.votes, game.mission_cards, game.quest_results, game.night_ack = [], {}, {}, [], set()
            game.last_vote_summary, game.last_mission_summary = "", ""
            game.notify("🌙 夜晚降临，请查看您的角色信息...", "purple")

    elif action == "night_ack":
        game.night_ack.add(player.id)
        if len(game.night_ack) >= len(game.players):
            game.phase = "quest"
            game.notify(f"☀️ 黎明降临！领袖 {game.leader().name} 请组建队伍", "blue")
        else:
            game.touch()

    elif action == "select_team" and player.id == game.leader().id and game.phase == "quest":
        game.team = data.get("team", [])
        game.votes, game.phase = {}, "vote"
        game.notify(f"👑 {player.name} 提名了队伍，全员开始投票...", "blue")

    elif action == "vote" and game.phase == "vote" and player.id not in game.votes:
        game.votes[player.id] = data.get("approve", True)
        if len(game.votes) >= len(game.players): process_vote(game)
        else: game.touch()

    elif action == "mission_card" and game.phase == "execute" and player.id in game.team and player.id not in game.mission_cards:
        card = True if ROLES[player.role]["team"] == "good" else data.get("success", True)
        game.mission_cards[player.id] = card
        if len(game.mission_cards) >= len(game.team): process_mission(game)
        else: game.touch()

    elif action == "assassinate" and game.phase == "assassinate" and ROLES[player.role]["team"] == "evil":
        target = game.get_player(data.get("target"))
        if target:
            game.assassin_target = target.id
            if target.role == "merlin":
                game.winner = "evil"
                game.notify(f"🎯 刺客刺杀了 {target.name}（梅林）！邪恶阵营逆转获胜！", "red")
            else:
                game.winner = "good"
                game.notify(f"🎯 刺客刺杀了 {target.name}，但TA不是梅林！正义阵营获胜！", "gold")
            game.phase = "end"

    elif action == "restart" and player.id == game.host_id:
        game.phase = "lobby"
        game.round, game.vote_reject_count, game.winner, game.assassin_target = 0, 0, None, None
        game.team, game.votes, game.mission_cards, game.quest_results, game.night_ack = [], {}, {}, [], set()
        game.last_vote_summary, game.last_mission_summary = "", ""
        for p in game.players: p.role = None
        game.notify(f"🔄 房主重置了游戏，回到大厅", "orange")

    return web.json_response({"status": "ok"})


async def index(request):
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(BASE_DIR, 'index.html')
    if os.path.exists(file_path):
        return web.FileResponse(file_path)
    return web.Response(text="⚠️ 找不到 index.html，请确保它和 server.py 放在同一个文件夹的根目录下！", status=404)

def build_app():
    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == 'OPTIONS':
            resp = web.Response()
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return resp
        try:
            resp = await handler(request)
        except Exception:
            raise
            
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get('/', index)
    app.router.add_get('/health', lambda r: web.json_response({"status": "ok"}))
    app.router.add_post('/api/room', api_create_room)
    app.router.add_post('/api/join', api_join)
    app.router.add_get('/api/sync', api_sync)
    app.router.add_post('/api/action', api_action)
    return app

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    print(f"🏰 Avalon HTTP Polling Server running on port {port}")
    web.run_app(build_app(), host='0.0.0.0', port=port)
