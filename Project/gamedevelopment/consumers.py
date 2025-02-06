# consumers.py
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async 

class GameConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        
        self.game_room_id = self.scope['url_route']['kwargs']['game_room_id']
        self.room_group_name = f'game_{self.game_room_id}'

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

    async def disconnect(self, close_code):
        # Leave room group
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    async def receive(self, text_data):
        text_data_json = json.loads(text_data)
        try:
            action = text_data_json.get('action')
            player_id = text_data_json.get('player_id')
        except KeyError as e:
            # Handle missing keys
            await self.send(text_data=json.dumps({'error': 'Invalid data received'}))
            return


        if action == 'place_bet':
            await self.handle_bet(player_id, text_data_json.get('amount'))
        elif action == 'pack':
            await self.handle_pack(player_id)
        elif action == 'sideshow':
            await self.handle_sideshow(player_id)

    # async def handle_bet(self, player_id, amount):
    #     player = await self.get_player(player_id)
    #     if player.balance >= amount:
    #         player.balance -= amount
    #         self.game_room.pot += amount
    #         await self.send_game_data()
    #     else:
    #         await self.send(text_data=json.dumps({"message": "Insufficient balance!"}))

    # async def handle_pack(self, player_id):
    #     """Handles when a player packs (folds)."""
    #     player = await self.get_player(player_id)
    #     player.status = "packed"
    #     await sync_to_async(player.save)()

    #     remaining_players = await sync_to_async(lambda: list(self.game_room.players.exclude(status="packed")))()
    #     if len(remaining_players) == 1:
    #         await self.declare_winner(remaining_players[0])

    #     await self.send_game_data()

    # async def handle_sideshow(self, player_id):
    #     """Handles sideshow request."""
    #     player = await self.get_player(player_id)
    #     active_players = await sync_to_async(lambda: list(self.game_room.players.exclude(status="packed")))()

    #     if len(active_players) >= 2:
    #         opponent = active_players[0] if active_players[0].id != player.id else active_players[1]
    #         winner = await self.compare_hands(player, opponent)

    #         if winner:
    #             await self.declare_winner(winner)
        
    #     await self.send_game_data()

    # async def declare_winner(self, player):
    #     """Declare the winner and distribute the pot."""
    #     player.balance += self.game_room.pot
    #     self.game_room.pot = 0
    #     await sync_to_async(player.save)()
    #     await sync_to_async(self.game_room.save)()

    #     await self.send(text_data=json.dumps({
    #         "message": f"{player.username} wins the round!",
    #         "winner": player.id
    #     }))

    # async def compare_hands(self, player1, player2):
    #     """Compares two players' hands and returns the winner."""
    #     hand1_value = await self.calculate_hand_value(player1.hand_cards)
    #     hand2_value = await self.calculate_hand_value(player2.hand_cards)

    #     return player1 if hand1_value > hand2_value else player2

    # async def calculate_hand_value(self, hand_cards):
    #     """Simple hand evaluation logic."""
    #     card_values = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
    #                    "J": 11, "Q": 12, "K": 13, "A": 14}
    #     return sum(card_values[card.split(" ")[0]] for card in hand_cards.split(","))

    # async def send_game_data(self):
    #     """Sends updated game state to all players."""
    #     game_data = {
    #         "game_room_id": self.game_room.id,
    #         "players": await self.get_players(),
    #         "pot": self.game_room.pot,
    #         "current_bet": 500,  # Example
    #     }
    #     await self.send(text_data=json.dumps(game_data))
    

    async def get_game_room(self, game_room_id):
        # Delay model import until necessary
        from .models import GameTable
        return await sync_to_async(GameTable.objects.get)(id=game_room_id)

    # async def get_player(self, player_id):
    #     return await sync_to_async(Player.objects.get)(id=player_id)

    # async def get_players(self):
    #     players = await sync_to_async(self.game_room.players.all)()
    #     return [{"id": player.id, "username": player.username, "status": player.status} for player in players]
