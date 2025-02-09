# consumers.py
import asyncio
from datetime import datetime
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
import logging

logger = logging.getLogger('gamedevelopment')

#Create your consumers.py here:
class GameConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.accept()
        self.game_room_id = self.scope['url_route']['kwargs']['game_room_id']
        self.room_group_name = f'game_{self.game_room_id}'

        self.game_room = await self.get_game_room(self.game_room_id)
        logger.debug(self.game_room)

        if not self.game_room:
            await self.send(text_data=json.dumps({"error": "Game room not found"}))
            await self.close()
            return

        self.player = await self.get_player(self.scope["user"], self.game_room)
        

        if not self.player:
            self.player = await self.create_player(self.scope["user"], self.game_room)
            logger.debug(self.player)

        # Rejoin the game group after a reload
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        
        # Notify that the player has reconnected
        await self.send(text_data=json.dumps({"status": "reconnected", "game_room_id": self.game_room_id}))

        await self.send_game_data()


    async def disconnect(self, close_code):
        """Handles player exit in real-time."""
        if self.game_room and self.player:
            await self.remove_player(self.player, self.game_room)

            # Check if the table is now empty
            remaining_players = await database_sync_to_async(self.game_room.players.count)()
            if remaining_players == 0:
                await database_sync_to_async(self.game_room.delete)()

             # Use sync_to_async for accessing player.user.id in async context
            player_id = await database_sync_to_async(lambda: self.player.user.id)()
            # Notify other players
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "player_disconnected",
                    "player_id": player_id,
                },
            )

        # Clean up WebSocket connection
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)


    async def player_disconnected(self, event):
        """Handles a player disconnect event and updates the UI."""
        await self.send(text_data=json.dumps({
            "type": "player_disconnected",
            "player_id": event["player_id"]
        }))

    @database_sync_to_async
    def get_player_user_id(self, player):
    #Fetch the player user id.
        return player.user.id
    

        # In send_game_data
    async def send_game_data(self):
        print("send_game_data function triggered.")
        players = await self.get_players()  # Fetch players

        # Debugging output
        logger.debug(f" Sending Game Data for Table {self.game_room.id}")
        logger.debug(f" Found {len(players)} players:")
        
        if players:
            for p in players:
                logger.debug(f"  - {p['username']} (ID: {p['id']}, Coins: {p['coins']},Avatar: {p['avatar']}, Position: {p['position']})")
        else:
            logger.debug("No players found for this game room.")
        
        game_data = {
            "type":"game_update",
            "game_room_id": self.game_room.id,
            "players": players,  # Ensure players are included in the message
            "table_pot":self.game_room.table_limit,
            "pot": self.game_room.current_pot,
            "current_turn": self.game_room.current_turn,
            "status": self.game_room.is_active
        }

        # Send data to WebSocket group
        await self.channel_layer.group_send(
            self.room_group_name,
            {"type": "game_update", "data": game_data}
        )

    async def game_update(self, event):
        """Handles a game update event and updates the UI."""
        await self.send(text_data=json.dumps(event["data"]))

    # Inside get_players
    async def get_players(self):
        """Fetches all players asynchronously."""
        logger.debug("Fetching players...")  # Log when the function is called

        players = await self._fetch_players()  # Call the sync DB query asynchronously

        # If no players are returned, this could indicate an issue with the relationship
        if not players:
            logger.debug(f"No players associated with game room {self.game_room.id}")
        
        player_data = []
        positions = ["top-left", "top-right", "bottom-left", "bottom-right"]  # Define available positions
        for idx, player in enumerate(players):
            player_info = {
                "id": player.user.id,
                "username": player.user.username,
                "coins": player.user.coins,
                "avatar": player.user.avatar.url if player.user.avatar else "/media/avatars/default.png",
                "position": positions[idx % len(positions)]  # Assign a position dynamically
            
            }
            player_data.append(player_info)

        return player_data


    @database_sync_to_async
    def _fetch_players(self):
        """Sync function to fetch players safely."""
        from .models import Player
       # Using select_related to optimize database queries
        players = list(Player.objects.select_related('user').filter(table=self.game_room))
        logger.debug(players)
        return players
        return players



    @database_sync_to_async
    def get_game_room(self, game_room_id):
        """Fetches the game room by ID."""
        from .models import GameTable
        try:
            game_room = GameTable.objects.get(id=game_room_id)
            logger.debug(f"Game Room {game_room_id} found: {game_room}")
            return game_room
        except ObjectDoesNotExist:
            logger.error(f"Game Room {game_room_id} not found!")
            return None


    @database_sync_to_async
    def get_player(self, user, game_room):
        # Fetches the player by user and game table.
        from .models import Player
        try:
            return Player.objects.get(user=user, table=game_room)
        except ObjectDoesNotExist:
            return None

    @database_sync_to_async
    def create_player(self, user, game_room):
        # """Creates a new player if not found."""
        from .models import Player
        if not game_room:
            raise ValueError("Invalid game room")
        return Player.objects.create(user=user, table=game_room)

    @database_sync_to_async
    def remove_player(self, player, game_room):
        """Removes a player from the game room."""
        player.delete()

    @database_sync_to_async
    def get_active_players_count(self):
        """Gets the count of players who haven't packed."""
        return self.game_room.players.exclude(is_packed=True).count()
    
    async def receive(self, text_data):
        try:
            text_data_json = json.loads(text_data)
            action = text_data_json.get('action')
            player_id = text_data_json.get('player_id')

            if action == 'place_bet':
                await self.handle_bet(player_id, text_data_json.get('amount'))
            elif action == 'pack':
                await self.handle_pack(player_id)
            elif action == 'sideshow':
                await self.handle_sideshow(player_id, text_data_json.get('opponent_id'))
            elif action == 'next_turn':
                await self.next_turn()

            # Ensure this is called to send the game data to WebSocket after any action
            await self.send_game_data()
        except IntegrityError as e:
            await self.send(text_data=json.dumps({'error': 'Database error: ' + str(e)}))
        except ObjectDoesNotExist as e:
            await self.send(text_data=json.dumps({'error': 'Object not found: ' + str(e)}))
        except Exception as e:
            await self.send(text_data=json.dumps({'error': str(e)}))

    # async def handle_bet(self, player_id, amount):
    #     player = await self.get_player(player_id)
    #     if player.balance >= amount:
    #         player.balance -= amount
    #         self.game_room.pot += amount
    #         self.game_room.current_turn = (self.game_room.current_turn + 1) % await self.get_active_players_count()
            
    #         await sync_to_async(player.save)()
    #         await sync_to_async(self.game_room.save)()
    #         await self.send_game_data()
    #     else:
    #         await self.send(text_data=json.dumps({'message': 'Insufficient balance!'}))
    
    # async def handle_pack(self, player_id):
    #     player = await self.get_player(player_id)
    #     player.status = "packed"
    #     await sync_to_async(player.save)()
        
    #     remaining_players = await self.get_active_players()
    #     if len(remaining_players) == 1:
    #         await self.declare_winner(remaining_players[0])
    #     else:
    #         await self.next_turn()
        
    #     await self.send_game_data()
    
    # async def handle_sideshow(self, player_id, opponent_id):
    #     player = await self.get_player(player_id)
    #     opponent = await self.get_player(opponent_id)
        
    #     winner = await self.compare_hands(player, opponent)
    #     await self.declare_winner(winner)
        
    #     await self.send_game_data()
    
    # async def next_turn(self):
    #     self.game_room.current_turn = (self.game_room.current_turn + 1) % await self.get_active_players_count()
    #     await sync_to_async(self.game_room.save)()
    #     await self.send_game_data()
    
    # async def declare_winner(self, player):
    #     player.balance += self.game_room.pot
    #     self.game_room.pot = 0
    #     self.game_room.status = "finished"
    #     await sync_to_async(player.save)()
    #     await sync_to_async(self.game_room.save)()
        
    #     await self.send(text_data=json.dumps({
    #         "message": f"{player.username} wins the round!",
    #         "winner": player.id
    #     }))
    
    # async def compare_hands(self, player1, player2):
    #     hand1_value = await self.calculate_hand_value(player1.hand_cards)
    #     hand2_value = await self.calculate_hand_value(player2.hand_cards)
    #     return player1 if hand1_value > hand2_value else player2
    
    # async def calculate_hand_value(self, hand_cards):
    #     card_values = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
    #                    "J": 11, "Q": 12, "K": 13, "A": 14}
    #     return sum(card_values[card.split(" ")[0]] for card in hand_cards.split(","))
