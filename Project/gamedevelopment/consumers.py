# consumers.py
import asyncio
from datetime import datetime
from asgiref.sync import sync_to_async  # Add this import
import json
import random
import sys
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
import logging
import sys
from django.db import transaction
import io
from django.db.models import F

# Configure logging to handle Unicode characters
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
# Force UTF-8 encoding for the console
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
logger = logging.getLogger('gamedevelopment')

#Create your consumers.py here:
class GameConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.game_room_id = self.scope['url_route']['kwargs']['game_room_id']
        self.room_group_name = f'game_{self.game_room_id}'

        self.game_room = await self.get_game_room(self.game_room_id)

        if not self.game_room:
            await self.send(text_data=json.dumps({"error": "Game room not found"}))
            await self.close()
            return

        self.player = await self.get_player(self.scope["user"], self.game_room)
        if not self.player:
            self.player = await self.create_player(self.scope["user"], self.game_room)
            logger.debug(f"Created player: {self.player}")
        
        await database_sync_to_async(self.game_room.refresh_from_db)()
        logger.debug(f"Connected to game room {self.game_room_id}: pot = {self.game_room.current_pot}, round_status = {self.game_room.round_status}, is_active = {self.game_room.is_active}")        
        await self.accept()
        
            # Set new player as spectator if game is in progress
        if self.game_room.round_status in ['betting', 'distribution']:
            self.player.is_packed = True
            await database_sync_to_async(self.player.save)(update_fields=['is_packed'])
            await self.send(text_data=json.dumps({
                    "type": "spectator_notification",
                    "message": "Game in progress. You are a spectator until the next round starts."
                }))
        
        # join the game group
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.send_game_data()


        # Only start the game if it hasn’t started yet and enough players are present
        player_count = await self.get_active_players_count()
        logger.debug(f"Player count: {player_count}")
        if player_count >= 2 and not self.game_room.is_active and self.game_room.round_status not in ['betting', 'distribution']:            
            logger.debug("Scheduling game start with 5-second delay")
            asyncio.create_task(self.delayed_start_game())  # Schedule with delay

    async def delayed_start_game(self):
        """Delays the game start by 5 seconds to allow more players to join."""
        # Check if game is already starting or started
        should_proceed = await self._check_and_lock_game_start()
        if not should_proceed:
            logger.debug("Game already starting or in progress, skipping delayed start")
            return

        logger.debug("Waiting 5 seconds before starting game...")
        await asyncio.sleep(5)  # Wait 5 seconds for more players to join

        # Recheck player count after delay
        player_count = await self.get_active_players_count()
        if player_count < 2:
            logger.debug("Player count dropped below 2 after delay, aborting start")
            await self._reset_is_active()
            return

        # Proceed with game start
        logger.debug(f"Starting game with {player_count} players after delay")
        await self.start_game()


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
        active_players = await self.get_active_players()  # Only active players
        active_player_ids = [p.user.id for p in active_players]  # IDs of active players
        # Refresh game room to ensure latest state
        await database_sync_to_async(self.game_room.refresh_from_db)()
        # Debugging output
        logger.debug(f" Sending Game Data for Table {self.game_room.id}")
        logger.debug(f" Found {len(players)} players:")
        
        if players:
            for p in players:
                logger.debug(f"  - {p['username']} (ID: {p['id']}, Coins: {p['coins']},Avatar: {p['avatar']}, Position: {p['position']}, Current Bet: {p['current_bet']})")
        else:
            logger.debug("No players found for this game room.")
        
        game_data = {
            "type":"game_update",
            "game_room_id": self.game_room.id,
            "players": players,  # Ensure players are included in the message
            "active_players": active_player_ids,  # Include active player IDs
            "table_pot":self.game_room.table_limit,
            "pot": self.game_room.current_pot,
            "current_turn": self.game_room.current_turn,
            "status": self.game_room.is_active,
            "round_status": self.game_room.round_status  # Include the round status here
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
                "position": positions[idx % len(positions)],  # Assign a position dynamically
                "cards": player.hand_cards,
                "is_blind": player.is_blind,
                "is_packed": player.is_packed,
                "current_bet": player.current_bet  # Ensure current_bet is included
            
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

    async def start_game(self):
        logger.debug(f"start_game called for room {self.game_room_id}")
        player_count = await self.get_active_players_count()
        logger.debug(f"Player count: {player_count}")
        await self.send_game_data()

        if player_count < 2:
            logger.debug("Not enough players to start the game.")
            await self._reset_is_active()
            return
            
                    # Step 3: Begin game initialization
        logger.debug("Game is ready to start!")
        await self.update_game_state("distribution")
        logger.debug(f"Game started: pot = {self.game_room.current_pot}, round_status = {self.game_room.round_status}, is_active = {self.game_room.is_active}")
        await self.distribute_cards(player_count)

        # Step 4: Apply boot amounts and reset player states
        active_players = await self.get_active_players()
        boot_amount = 100
        for player in active_players:
            try:
                logger.debug(f"Processing boot for {player.user.username}, coins before: {player.user.coins}")
                player.is_packed = False                
                player.current_bet = boot_amount
                await self.update_player_coins(player, -boot_amount)
                await self.update_game_room_pot(boot_amount)
                await database_sync_to_async(player.save)()
                logger.debug(f"Boot applied for {player.user.username}, coins after: {player.user.coins}")
            except Exception as e:
                logger.error(f"Error applying boot for {player.user.username}: {e}")
        # Step 5: Finalize game start
        await self.update_game_state("betting")
        logger.debug(f"Starting game with pot = {self.game_room.current_pot}")
        self.game_room.current_turn = 0
        await database_sync_to_async(self.game_room.save)(update_fields=['current_turn'])
        await self.send_game_data()

# Helper method to check and lock game start atomically
    @database_sync_to_async
    def _check_and_lock_game_start(self):
        with transaction.atomic():
            self.game_room.refresh_from_db()
            logger.debug(f"Game room {self.game_room.id}: round_status={self.game_room.round_status}, is_active={self.game_room.is_active}")            
            if self.game_room.round_status in ['betting', 'distribution']:
                return False
            self.game_room.is_active = True
            self.game_room.save(update_fields=['is_active'])
            return True

        # Helper method to reset is_active
    
    @database_sync_to_async
    def _reset_is_active(self):
            self.game_room.is_active = False
            self.game_room.save(update_fields=['is_active'])
    
    async def distribute_cards(self, active_players_count):
        """Distribute cards to the players based on the active player count."""
        logger.debug(f"Distributing cards to {active_players_count} players.")
        players = await self.get_active_players()  # Use actual Player model instances
        if len(players) < 2:
            logger.debug("Not enough players to distribute cards.")
            return
        
        suits = ['♠', '♥', '♦', '♣']
        ranks = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
        deck = [rank + suit for suit in suits for rank in ranks]
        random.shuffle(deck)

        cards_per_player = 3
        for player in players:
            player_cards = deck[:cards_per_player]
            deck = deck[cards_per_player:]
            await self.assign_card_to_player(player.user.id, player_cards)
            logger.debug(f"Player {player.user.username} got {player_cards}")

    async def assign_card_to_player(self, player_id, cards):
        player = await self.get_player_by_id(player_id)
        if player:
            player.hand_cards = list(cards)
            await self.save_player(player)
            logger.debug(f"Assigned and saved cards for {player.user.username}: {player.hand_cards}")
            # Verify save
            player = await self.get_player_by_id(player_id)
            if player.hand_cards == cards:
                logger.debug(f"Verified: Cards {cards} successfully saved for {player.user.username}")
            else:
                logger.error(f"Failed to save cards for {player.user.username}: Expected {cards}, got {player.hand_cards}")

    @database_sync_to_async
    def save_player(self, player):
        with transaction.atomic():
            player.save()
            logger.debug(f"Player {player.user.username} saved with hand_cards: {player.hand_cards}")

    @database_sync_to_async
    def get_player_by_id(self, player_id):
        from .models import Player
        """Fetch a player by their user's ID and table."""
        try:
            return Player.objects.select_related('user').get(user__id=player_id, table=self.game_room)
        except Player.DoesNotExist:
            logger.error(f"Player with user ID {player_id} does not exist in table {self.game_room.id}!")
            return None
        except Player.MultipleObjectsReturned:
            logger.error(f"Multiple Player entries found for user ID {player_id} in table {self.game_room.id}.")
            return None


    async def dispatch(self, message):
        message_type = message.get('type')
        if not message_type:
            logger.error(f"Message missing 'type': {message}")
            await self.send(text_data=json.dumps({'error': 'Invalid message format'}))
            return
        
        handler = getattr(self, message_type.replace('.', '_'), None)
        if callable(handler):
            logger.debug(f"Calling handler: {message_type}")
            await handler(message)
        else:
            logger.error(f"No callable handler for type {message_type}")
            await self.send(text_data=json.dumps({'error': f'No handler for {message_type}'}))

    async def receive(self, text_data):
        from .models import Player
        try:
            text_data_json = json.loads(text_data)
            logger.debug(f"Received client message: {text_data_json}")
            action = text_data_json.get('action')
            player_id = text_data_json.get('player_id')
            bet_amount = text_data_json.get('amount')
            player = await database_sync_to_async(Player.objects.get)(user__id=player_id, table=self.game_room)
            
            if action == 'place_bet':
                await self.handle_bet(player_id, text_data_json.get('amount'))
            elif action == 'place_double_bet':
                await self.handle_double_bet(player_id,text_data_json.get('amount'))
            elif action == 'pack':
                await self.handle_pack(player_id)
            elif action == 'sideshow':
                await self.handle_sideshow(player_id, text_data_json.get('opponent_id'),bet_amount)
            elif action == 'sideshow_response':
                await self.handle_sideshow_response(player_id, text_data_json.get('accept'))
            elif action == 'toggle_seen':
                await self.toggle_seen(player)
            elif action == 'show':
                await self.handle_show(player_id)
            else:
                await self.send_game_data()
  
        except IntegrityError as e:
            await self.send(text_data=json.dumps({'error': 'Database error: ' + str(e)}))
        except ObjectDoesNotExist as e:
            await self.send(text_data=json.dumps({'error': 'Object not found: ' + str(e)}))
        except Exception as e:
            await self.send(text_data=json.dumps({'error': str(e)}))

    
    @database_sync_to_async
    def get_game_room(self, game_room_id):
        """Fetches the game room by ID."""
        from .models import GameTable
        try:
            game_room = GameTable.objects.get(id=game_room_id)
            logger.debug(f"Fetched game room {game_room_id}: pot = {game_room.current_pot}, round_status = {game_room.round_status}")
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

    async def get_active_players(self):
        from .models import Player
        return await database_sync_to_async(lambda: list(Player.objects.filter(
            table=self.game_room, is_packed=False
        ).select_related('user')))()

    @database_sync_to_async
    def get_active_players_count(self):
        """Gets the count of players who haven't packed."""
        count=self.game_room.players.exclude(is_packed=True).count()
        logger.debug(f"Active players count: {count}")
        return count

    @database_sync_to_async
    def update_game_state(self, state):

        """Updates the game state."""
        self.game_room.round_status = state
        self.game_room.save()
        logger.debug(f"Game state updated to {self.game_room.round_status}")

    async def handle_bet(self, player_id, amount):
        logger.debug(f"Fetching player by ID: {player_id}")
        player = await self.get_player_by_id(player_id)
        if player:
                logger.debug(f"Player found: {player.user.username} with {player.user.coins} coins")
        else:
            logger.debug("Player not found")
            await self.send(text_data=json.dumps({'message': 'Player not found!'}))
            return
        
        active_players = await database_sync_to_async(list)(self.game_room.players.filter(is_packed=False))
        current_turn_index = self.game_room.current_turn
        # if active_players[current_turn_index].id != player.id:
        #     logger.debug("Not player's turn")
        #     return

        # Get previous player's bet for Teen Patti rules
        prev_index = (current_turn_index - 1) % len(active_players)
        prev_player = active_players[prev_index]
        min_bet = prev_player.current_bet if prev_player.is_blind else prev_player.current_bet // 2
        # Minimum bet calculation
        if player.is_blind:
            min_bet = prev_player.current_bet if prev_player.is_blind else prev_player.current_bet // 2
        else:  # Seen player
            min_bet = prev_player.current_bet * 2 if prev_player.is_blind else prev_player.current_bet
        
        if amount < min_bet:
            await self.send(text_data=json.dumps({'message': f'Bet must be at least {min_bet}'}))
            return
        user_coins = await database_sync_to_async(lambda: player.user.coins)()
        if user_coins >= amount:
            logger.debug(f"Player {player.user.username}")
            await self.update_player_coins(player, -amount)
            logger.debug(f"Amount:{amount}")
            await self.update_game_room_pot(amount,context='handle_bet')
            await self.save_current_bet(player, amount)
            await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])
            active_players_count = await self.get_active_players_count()
            await self.next_turn(active_players_count)
            await self.update_game_state("betting")
            if active_players_count <= 1:
                await self.declare_winner()
            else:
                await self.send_game_data()
        else:
            logger.debug(f"Insufficient balance for {player.user.username}: {player.user.coins} < {amount}")
            await self.send(text_data=json.dumps({'message': 'Insufficient balance!'}))


    async def handle_double_bet(self, player_id, amount):  # Add amount parameter
        player = await self.get_player_by_id(player_id)
        if not player:
            return

        active_players = await database_sync_to_async(list)(self.game_room.players.filter(is_packed=False))
        current_turn_index = self.game_room.current_turn

        prev_index = (current_turn_index - 1) % len(active_players)
        prev_player = active_players[prev_index]
        
        logger.debug(f"Final bet amount: {amount}")
        user_coins = await database_sync_to_async(lambda: player.user.coins)()
        if user_coins >= amount:
            await self.update_player_coins(player, -amount)
            await self.update_game_room_pot(amount,context='handle_double_bet')
            await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])
            await self.save_current_bet(player, amount)
            active_players_count = await self.get_active_players_count()
            await self.next_turn(active_players_count)
            await self.update_game_state("betting")

            # if active_players_count <= 1:
            #     await self.declare_winner()
            # else:
            await self.send_game_data()
        else:
            await self.send(text_data=json.dumps({'message': f'Insufficient balance for double bet! Need {amount}, have {user_coins}'}))
    
    @database_sync_to_async
    def update_player_coins(self, player, amount):
        with transaction.atomic():
            player.user.refresh_from_db()  # Get latest coins before update
            logger.debug(f"Before update - Coins for {player.user.username}: {player.user.coins}")
            player.user.coins += amount  # Amount is negative, deducts
            player.user.save(update_fields=['coins'])
            player.user.refresh_from_db()  # Ensure latest value
            logger.debug(f"After update - Coins for {player.user.username}: {player.user.coins}")

    @database_sync_to_async
    def update_game_room_pot(self, amount, context=''):
        self.game_room.refresh_from_db()
        with transaction.atomic():
            logger.debug(f"[{context}] Before pot update: {self.game_room.current_pot}")
            if self.game_room.current_pot is None:
                self.game_room.current_pot = 0
            old_pot = self.game_room.current_pot
            self.game_room.current_pot += amount
            logger.debug(f"[{context}] Pot update: {old_pot} + {amount} = {self.game_room.current_pot}")
            self.game_room.save(update_fields=['current_pot'])


    @database_sync_to_async
    def save_current_bet(self, player, amount): 
        with transaction.atomic():
            player.refresh_from_db()  # Get latest current_bet before update
            logger.debug(f"Before update - Current bet for {player.user.username}: {player.current_bet}")
            player.current_bet = amount  # Accumulate bets, not overwrite
            player.save(update_fields=['current_bet'])
            player.refresh_from_db()  # Ensure latest value
            logger.debug(f"After update - Current bet for {player.user.username}: {player.current_bet}")


    async def handle_pack(self, player_id):
        player = await self.get_player_by_id(player_id)
        if not player:
            return
        
        if player.is_packed:
            logger.debug(f"Player {player.user.username} is already packed, ignoring action")
            return
        # Refresh game room state to ensure we have the latest pot value
        await database_sync_to_async(self.game_room.refresh_from_db)()
        current_pot = self.game_room.current_pot
        logger.debug(f"Before packing: pot = {current_pot}, player = {player.user.username}")
        player.is_packed = True
        await database_sync_to_async(player.save)()
        await self.send_game_data()
        
        remaining_players = await self.get_active_players()
        initial_player_count = await self.get_active_players_count() + 1  # Include the packing player
        active_players_count = len(remaining_players)
        logger.debug(f"Active players after pack: {active_players_count}, initial count: {initial_player_count}")

        if initial_player_count == 2 and active_players_count == 1:
            logger.debug(f"Only one player left with initial two players, declaring winner: {remaining_players[0].user.username}")
            await self.declare_winner(remaining_players[0])
        else:
            logger.debug(f"Multiple players remain, advancing turn")
            if active_players_count <= 1:
                if remaining_players:
                    logger.debug(f"Only one player left, declaring winner: {remaining_players[0].user.username}")
                    await self.declare_winner(remaining_players[0])
            else:
                # If the current turn is the packed player, advance immediately
                active_players = await self.get_active_players()
                if self.game_room.current_turn >= active_players_count:
                    self.game_room.current_turn = 0
                    logger.debug(f"Reset turn to 0 due to packed player")
                else:
                    current_turn_player = active_players[self.game_room.current_turn]
                    if current_turn_player.user.id == player.user.id:
                        logger.debug(f"Packed player was current turn, advancing turn")
                        await self.next_turn(active_players_count)
                    else:
                        logger.debug(f"Advancing turn normally")
                        await self.next_turn(active_players_count)
                # Preserve game state
                await self.update_game_state("betting")
                # Check pot integrity
                await database_sync_to_async(self.game_room.refresh_from_db)()
                if self.game_room.current_pot != current_pot:
                    logger.warning(f"Pot changed from {current_pot} to {self.game_room.current_pot} during pack, restoring")
                    self.game_room.current_pot = current_pot
                    await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])
        
        logger.debug(f"After packing: pot = {self.game_room.current_pot}")
        

    async def next_turn(self, active_players_count):
        players = await self.get_active_players()
        current_turn = self.game_room.current_turn
        
        if active_players_count <= 0:
            logger.error("No active players to advance turn")
            await self.send(text_data=json.dumps({"error": "No active players left"}))
            return
        
        if current_turn >= active_players_count or current_turn < 0:
            current_turn = 0
        
        # Advance to the next non-packed player
        next_index = (current_turn + 1) % active_players_count
        for i in range(active_players_count):
            candidate_index = (current_turn + i + 1) % active_players_count
            if not players[candidate_index].is_packed:
                next_index = candidate_index
                break
        
        self.game_room.current_turn = next_index
        await database_sync_to_async(self.game_room.save)()
        logger.debug(f"Turn advanced from {current_turn} to index {next_index}, player ID: {players[next_index].user.id}, is_packed: {players[next_index].is_packed}")        
        # Check game end condition
        if active_players_count <= 1:
            remaining_players = await self.get_active_players()
            if len(remaining_players) == 1:
                logger.debug(f"One player remains: {remaining_players[0].user.id}")
                await self.declare_winner(remaining_players[0])
                return
        
        await self.send_game_data()

    async def handle_sideshow(self, player_id, opponent_id,bet_amount):
        await self.send_game_data()
        from django.core.cache import cache
        player = await self.get_player_by_id(player_id)
        opponent = await self.get_player_by_id(opponent_id)
        if not player or not opponent:
            await self.send(text_data=json.dumps({'message': 'Player or opponent not found!'}))
            return

        # Prevent packed players from requesting sideshows
        if player.is_packed:
            await self.send(text_data=json.dumps({'message': 'Packed players cannot request a sideshow!'}))
            return
        
        await sync_to_async(self.game_room.refresh_from_db)()        
        active_players = await self.get_active_players()
        current_turn_index = self.game_room.current_turn
        # if active_players[current_turn_index].user.id != player.user.id:
        #     await self.send(text_data=json.dumps({'message': 'Not your turn!'}))
        #     return

        # Check if opponent is the previous active player
        prev_index = (current_turn_index - 1) % len(active_players)
        logger.debug(f"Active players: {[p.user.id for p in active_players]}")
        logger.debug(f"Current turn index: {current_turn_index}")
        logger.debug(f"Prev turn index: {prev_index}")
        logger.debug(f"Prev turn index player: {active_players[prev_index].user.id}")
        logger.debug(f"Opponent ID: {opponent.user.id}")
        if active_players[prev_index].user.id != opponent.user.id:
            await self.send(text_data=json.dumps({'message': 'Sideshow can only be requested with the previous player!'}))
            return

        # Check if player has bet enough
        if bet_amount is not None:
            if player.user.coins < bet_amount:
                await self.send(text_data=json.dumps({'message': 'Insufficient coins for sideshow bet!'}))
                return
            await self.update_player_coins(player, -bet_amount)  # Deduct coins
            await self.save_current_bet(player, opponent.current_bet)  # Set current_bet
            await self.update_game_room_pot(bet_amount,context = "handle_sideshow")  # Add to pot
            logger.debug(f"Player {player.user.username} bet {bet_amount} to match {opponent.user.username}")
        
        if player.current_bet < opponent.current_bet:
            await self.send(text_data=json.dumps({'message': 'You must match the previous bet for a sideshow!'}))
            return
        
        if self.game_room.round_status != 'betting':
            await self.send(text_data=json.dumps({'message': 'Sideshow can only be requested during betting!'}))
            return
            # Add blind/seen check
        if player.is_blind or opponent.is_blind:
            await self.send(text_data=json.dumps({'message': 'Sideshow is only available between seen players!'}))
            return
        # Store sideshow request in cache
        sideshow_key = f"sideshow_{self.game_room_id}_{player_id}_{opponent_id}"
        cache.set(sideshow_key, {'player_id': str(player_id), 'opponent_id': str(opponent_id), 'accepted': None}, timeout=15)
        logger.debug(f"Sideshow requested by {player.user.username} against {opponent.user.username}, key: {sideshow_key}")
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "sideshow_request",
                "requester_id": player_id,
                "opponent_id": opponent_id,
                "sideshow_key": sideshow_key 
            }
        )

        # Notify requester that request is pending
        await self.send(text_data=json.dumps({'message': f'Sideshow request sent to {opponent.user.username}. Awaiting response...'}))

        # Wait for response with a timeout (e.g., 10 seconds)
        timeout = 15
        for _ in range(timeout * 2):  # Check every 0.5 seconds
            request = cache.get(sideshow_key)
            if request and request['accepted'] is not None:
                break
            await asyncio.sleep(0.5)

        request = cache.get(sideshow_key) or {'accepted': None}  # Fallback if cache expires
        active_players_count = await self.get_active_players_count()
        if request['accepted'] is None:
            logger.debug(f"Sideshow request to {opponent.user.username} (timeout)")
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "sideshow_result",
                    "message": f"Sideshow declined by {opponent.user.username} (timeout)",
                    "packed_player": None
                }
            )

        elif request['accepted']:
            # Compare hands if accepted
            winner = await self.compare_hands(player, opponent)
            loser = opponent if winner == player else player
            loser.is_packed = True
            await database_sync_to_async(loser.save)()

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "sideshow_result",
                    "message": f"Sideshow: {winner.user.username} wins against {loser.user.username}!",
                    "winner_id": winner.user.id,
                    "loser_id": loser.user.id,
                    "packed_player": loser.user.id
                }
            )
        else:
            # Declined
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "sideshow_result",
                    "message": f"Sideshow declined by {opponent.user.username}",
                    "packed_player": None
                }
            )
        cache.delete(sideshow_key)



        # Always advance turn unless game ends
        if active_players_count > 1:
            await self.next_turn(active_players_count)
        else:
            await self.declare_winner()
        await self.send_game_data()

    async def sideshow_request(self, event):
        """Handle sideshow request sent to the opponent."""
        logger.debug(f"Sideshow request event: {event}")
        await self.send(text_data=json.dumps({
            "type": "sideshow_request",
            "requester_id": event["requester_id"],
            "opponent_id": event["opponent_id"],
            "sideshow_key": event["sideshow_key"]
        }))

    async def handle_sideshow_response(self, player_id, accept):
        from django.core.cache import cache
        logger.debug(f"Handling sideshow response: player_id={player_id}, accept={accept}")
        opponent = await self.get_player_by_id(player_id)
        if not opponent:
            logger.error("Opponent not found")
            await self.send(text_data=json.dumps({'message': 'Invalid sideshow response!'}))
            return
        
        player_id = str(player_id)
        # Find the sideshow request in cache
        sideshow_key = None
        for key in cache.keys(f"sideshow_{self.game_room_id}_*_{player_id}"):
            sideshow_key = key
            break

        if not sideshow_key:
            logger.error("No active sideshow request found in cache")
            await self.send(text_data=json.dumps({'message': 'No active sideshow request!'}))
            return

        request = cache.get(sideshow_key)
        logger.debug(f"Retrieved cache data for {sideshow_key}: {request}")
        if not request:
            logger.error(f"Cache returned None for key {sideshow_key}")            
            await self.send(text_data=json.dumps({'message': 'Invalid sideshow response!'}))
            return
        
        # Ensure opponent_id is treated as a string
        cached_opponent_id = str(request.get('opponent_id', ''))
        if cached_opponent_id != player_id:
            logger.error(f"Player {player_id} is not the opponent for request: {request}")
            await self.send(text_data=json.dumps({'message': 'Invalid sideshow response!'}))
            return
        
        request['accepted'] = accept
        cache.set(sideshow_key, request, timeout=15)  # Update cache with response
        logger.debug(f"Sideshow response from {opponent.user.username}: {'Accepted' if accept else 'Declined'}")
    
    async def sideshow_result(self, event):
        """Broadcast sideshow result to all players."""
        logger.debug(f"Sideshow request event: {event}")
        await self.send(text_data=json.dumps(event))

    
    async def handle_show(self, player_id):
        await database_sync_to_async(self.game_room.refresh_from_db)()
        logger.debug(f"Initial current_pot in handle_show: {self.game_room.current_pot}")
        await self.update_game_state("showdown")
        player = await self.get_player_by_id(player_id)
        if not player:
            return
        
        active_players = await self.get_active_players()
        if len(active_players) != 2:
            await self.send(text_data=json.dumps({'message': 'Show can only be called with exactly 2 players!'}))
            return
        
        if self.game_room.round_status != 'showdown':
            logger.debug(f"Invalid game state: {self.game_room.round_status}")
            await self.send(text_data=json.dumps({'message': 'Show can only be called during showdown!'}))
            return

        # Calculate required stake (current stake if blind, double if seen)
        current_turn_index = self.game_room.current_turn
        prev_index = (current_turn_index - 1) % len(active_players)
        prev_player = active_players[prev_index]
        amount = (prev_player.current_bet 
          if player.is_blind or (not player.is_blind and not prev_player.is_blind) 
          else 2 * prev_player.current_bet)
        user_coins = await database_sync_to_async(lambda: player.user.coins)()
        if user_coins < amount:
            logger.debug(f"Insufficient balance for {player.user.username}: {user_coins} < {amount}")
            await self.send(text_data=json.dumps({
                'message': f'Insufficient coins! Need {amount}, have {user_coins}'
            }))
            return

         # Deduct stake and update pot
        if user_coins >= amount:
            logger.debug(f"Deducting stake {amount} from {player.user.username}")
            await database_sync_to_async(self.game_room.refresh_from_db)()
            await self.update_player_coins(player, -amount)
            await self.update_game_room_pot(amount,context='handle_show')
            await self.save_current_bet(player, amount)

        await self.send_game_data()

        logger.debug(f"Initiating SHOW with player: {player.user.username} (ID: {player.user.id})")
        logger.debug(f"Active players: {[p.user.username + ' (ID: ' + str(p.user.id) + ')' for p in active_players]}")

        try:
            opponent = next(p for p in active_players if p.user.id != player.user.id)
            logger.debug(f"Selected opponent: {opponent.user.username} (ID: {opponent.user.id})")
            logger.debug(f"{player.user.username}'s hand: {player.hand_cards}")
            logger.debug(f"{opponent.user.username}'s hand: {opponent.hand_cards}")
        except StopIteration:
            logger.error("No distinct opponent found in active_players")
            await self.send(text_data=json.dumps({'message': 'Error: No valid opponent for showdown!'}))
            return

        winner = await self.compare_hands(player, opponent)
        loser = opponent if winner.user.id == player.user.id else player
        logger.debug(f"Winner: {winner.user.username}, Loser: {loser.user.username}")

        # Save to GameHistory
        await self.save_game_history(winner, 'show', self.game_room.current_pot)

        # Declare winner and update coins
        logger.debug(f"Awarding pot of {self.game_room.current_pot} to {winner.user.username}")
        winner.user.coins += self.game_room.current_pot
        await database_sync_to_async(winner.user.save)()
        await database_sync_to_async(self.game_room.save)()
        self.game_room.current_pot = 0
        await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])
        logger.debug(f"Pot reset to {self.game_room.current_pot}, winner {winner.user.username} awarded")
        await self.channel_layer.group_send(
        self.room_group_name,
        {
        "type": "show_result",
        "message": f'Show: {winner.user.username} wins the pot!',
        "winner_id": winner.user.id,
        "hand_winner": winner.hand_cards,
        "hand_loser": opponent.hand_cards
    }
    )
        logger.debug("Sent show_result message")

        # Schedule new round after a 3-second delay
        logger.debug("Scheduling new round after showdown with 5-second delay")
        await asyncio.sleep(5)  # Wait 5 seconds to allow players to see the result
        logger.debug("Executing restart_round after delay")
        self.game_room.is_active = False  # Reset is_active before restarting
        await database_sync_to_async(self.game_room.save)(update_fields=['is_active'])
        await self.restart_round()
    


    async def restart_round(self):
        logger.debug(f"Restarting round for game room {self.game_room_id}")

        # Fetch all players, not just active ones, to reset their states
        all_players = await database_sync_to_async(lambda: list(self.game_room.players.all()))()
        player_count = len(all_players)
        logger.debug(f"Player count for new round: {player_count}")
        if player_count < 2:
            logger.debug("Not enough players to restart the round.")
            await self.update_game_state("waiting")
            await self.send(text_data=json.dumps({
                'message': 'Waiting for more players to join!'
            }))
            await self._reset_is_active()
            return

        @database_sync_to_async
        def reset_players(players, boot_amount):
            with transaction.atomic():
                for player in players:
                    player.is_packed = False
                    player.is_blind = True
                    player.current_bet = 0
                    player.hand_cards = []
                    player.save()
                    logger.debug(f"Reset player {player.user.username}, hand_cards: {player.hand_cards}")

        
        await reset_players(all_players, boot_amount=100)

        # Set is_active = True for the new round
        self.game_room.is_active = True
        await database_sync_to_async(self.game_room.save)(update_fields=['is_active'])
        # Additional logging to verify hand_cards are cleared after reset
        @database_sync_to_async
        def log_player_cards(players):
            for player in players:
                player.refresh_from_db() 
                logger.debug(f"After reset - Player {player.user.username} hand_cards: {player.hand_cards}")

        await log_player_cards(all_players)

        active_players = await self.get_active_players()
        logger.debug(f"Active players after reset: {[p.user.username for p in active_players]}, count: {len(active_players)}")

        self.game_room.current_pot = 0
        self.game_room.current_turn = 0
        await self.update_game_state("distribution")
        await database_sync_to_async(self.game_room.save)(update_fields=['current_pot', 'current_turn'])

        await self.distribute_cards(player_count)

        # Additional logging to verify hand_cards after card distribution
        await log_player_cards(active_players)

        boot_amount = 100
        for player in active_players:
            try:
                logger.debug(f"Processing boot for {player.user.username}, coins before: {player.user.coins}")
                if player.user.coins < boot_amount:
                    logger.debug(f"Insufficient coins for {player.user.username}, packing player")
                    player.is_packed = True
                    await database_sync_to_async(player.save)()
                    continue

                if player.current_bet == boot_amount:
                    continue

                player.current_bet = boot_amount
                await self.update_player_coins(player, -boot_amount)
                await self.update_game_room_pot(boot_amount)
                await database_sync_to_async(player.save)()
                logger.debug(f"Boot applied for {player.user.username}, coins after: {player.user.coins}")
            except Exception as e:
                logger.error(f"Error applying boot for {player.user.username}: {e}")

        player_count = await self.get_active_players_count()
        if player_count < 2:
            logger.debug("Not enough players after boot, ending round")
            await self.update_game_state("waiting")
            await self.send(text_data=json.dumps({
                'message': 'Not enough players to continue, waiting for more players!'
            }))
            return

        await self.update_game_state("betting")
        logger.debug(f"New round started with pot = {self.game_room.current_pot}, is_active = {self.game_room.is_active}")        
        self.game_room.is_active = False
        await database_sync_to_async(self.game_room.save)(update_fields=['is_active'])
        logger.debug(f"New round: is_active = {self.game_room.is_active}, round_status = {self.game_room.round_status}")

        await self.send_game_data()


    async def toggle_seen(self, player):
        # Ensure self.game_room reflects the latest database state
        await database_sync_to_async(self.game_room.refresh_from_db)()
        logger.debug(f"Refreshed game room: pot = {self.game_room.current_pot}, round_status = '{self.game_room.round_status}'")
        
        current_pot = self.game_room.current_pot if self.game_room.current_pot is not None else 0
        current_status = self.game_room.round_status
        logger.debug(f"Before toggle_seen: pot = {current_pot}, round_status = '{current_status}'")
        
        player.is_blind = False
        await database_sync_to_async(player.save)()
        
        await database_sync_to_async(self.game_room.refresh_from_db)()
        if self.game_room.current_pot != current_pot:
            logger.warning(f"Pot changed from {current_pot} to {self.game_room.current_pot}. Restoring...")
            self.game_room.current_pot = current_pot
            await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])
        if self.game_room.round_status != current_status:
            logger.warning(f"Round status changed from {current_status} to {self.game_room.round_status}. Restoring...")
            await self.update_game_state(current_status)
        
        logger.debug(f"After toggle_seen: pot = {self.game_room.current_pot}, round_status = '{current_status}'")
        await self.send_game_data()

    async def compare_hands(self, player1, player2):
        hand1_value = await self.calculate_hand_value(player1.hand_cards)
        hand2_value = await self.calculate_hand_value(player2.hand_cards)
        if hand1_value['rank'] == hand2_value['rank']:
            if hand1_value['high_card'] == hand2_value['high_card']:
                return player1  # Tiebreaker: First player wins (simplified)
            return player1 if hand1_value['high_card'] > hand2_value['high_card'] else player2
        return player1 if hand1_value['rank'] > hand2_value['rank'] else player2

    async def calculate_hand_value(self, hand_cards):
        """Calculate hand value based on Teen Patti rules."""
        card_values = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
                       "J": 11, "Q": 12, "K": 13, "A": 14}
        ranks = [card[:-1] for card in hand_cards]
        suits = [card[-1] for card in hand_cards]
        values = sorted([card_values[rank] for rank in ranks], reverse=True)

        # Check for Trail
        if len(set(ranks)) == 1:
            return {'rank': 6, 'high_card': values[0]}  # Trail

        # Check for Pure Sequence
        is_sequence = all(values[i] - 1 == values[i + 1] for i in range(len(values) - 1))
        is_same_suit = len(set(suits)) == 1
        if is_sequence and is_same_suit:
            return {'rank': 5, 'high_card': values[0]}  # Pure Sequence
        elif is_sequence:
            return {'rank': 4, 'high_card': values[0]}  # Sequence

        # Check for Color
        if is_same_suit:
            return {'rank': 3, 'high_card': values[0]}  # Color

        # Check for Pair
        if len(set(ranks)) == 2:
            pairs = [r for r in set(ranks) if ranks.count(r) == 2]
            if pairs:
                pair_value = card_values[pairs[0]]
                return {'rank': 2, 'high_card': pair_value}  # Pair

        # High Card
        return {'rank': 1, 'high_card': values[0]}

    @database_sync_to_async
    def save_game_history(self, player, action, amount):
        from .models import GameHistory
        GameHistory.objects.create(game=self.game_room,player=player,action=action,amount=amount)

    async def show_result(self, event):
        logger.debug(f"Sending show_result to client: {event['message']}")
        await self.send(text_data=json.dumps({
            "type": "show_result",
            "message": event["message"],
            "winner_id": event["winner_id"],
            "hand_winner": event["hand_winner"],
            "hand_loser": event["hand_loser"]
        }))

    async def declare_winner(self, winner):
        """Declares the winner, awards the pot, and prepares for the next round."""
        logger.debug(f"Declaring winner: {winner.user.username}")
        
        # Award the pot to the winner
        pot = self.game_room.current_pot if self.game_room.current_pot is not None else 0
        if pot > 0:
            logger.debug(f"Awarding pot of {pot} to {winner.user.username}")
            winner.user.coins += pot
            await database_sync_to_async(winner.user.save)(update_fields=['coins'])
            self.game_room.current_pot = 0
            await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])

        # Save to game history
        await self.save_game_history(winner, 'win', pot)

        # Notify all players
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "show_result",
                "message": f'{winner.user.username} wins the pot!',
                "winner_id": winner.user.id,
                "hand_winner": winner.hand_cards,
                "hand_loser": []  # No opponent in this case
            }
        )

        # Schedule the next round after a delay
        logger.debug("Scheduling restart_round with 5-second delay")
        await asyncio.sleep(5)  # Allow time to display the result
        self.game_room.is_active = False  # Reset is_active before restarting
        await database_sync_to_async(self.game_room.save)(update_fields=['is_active'])
        await self.restart_round()