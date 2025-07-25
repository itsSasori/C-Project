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

# Global timer store by game_room_id
TURN_TIMERS = {}
TURN_START_TIMES = {}

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

        try:
            self.player = await self.get_player(self.scope["user"], self.game_room)
            if not self.player:
                self.player = await self.create_player(self.scope["user"], self.game_room)
                logger.debug(f"Created player: {self.player}")
        except ValueError as e:
            await self.accept()
            await self.send(text_data=json.dumps({"error": str(e)}))
            await self.close()
            return
        
        await database_sync_to_async(self.game_room.refresh_from_db)()
        logger.debug(f"Connected to game room {self.game_room_id}: pot = {self.game_room.current_pot}, round_status = {self.game_room.round_status}, is_active = {self.game_room.is_active}")        
        await self.accept()
        
            # Set new player as spectator if game is in progress
        if self.game_room.round_status in ['betting', 'distribution']:
            self.player.is_spectator = True  # Use is_spectator 
            await database_sync_to_async(self.player.save)(update_fields=['is_spectator'])
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
        await self.cancel_timer()
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

    async def start_timer(self, player):
        from datetime import datetime, timezone
        """Start a 30-second timer for the player's turn."""
        logger.debug(f"Starting timer for player {player.user.username} (ID: {player.user.id}) with duration 30s")

        # Cancel existing timer for this room if it exists
        old_timer = TURN_TIMERS.get(self.game_room_id)
        if old_timer and not old_timer.done():
            old_timer.cancel()
            try:
                await old_timer
            except asyncio.CancelledError:
                logger.debug("Old timer cancelled.")

        TURN_START_TIMES[self.game_room_id] = datetime.now(timezone.utc)
        # Start new timer and store it
        task = asyncio.create_task(self.timer_task(player))
        TURN_TIMERS[self.game_room_id] = task

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "timer_start",
                "player_id": player.user.id,
                "duration": 30
            }
        )


    async def timer_task(self, player):
        """Task to handle turn timeout."""
        try:
            logger.debug(f"Starting 30-second timer for player {player.user.username} (ID: {player.user.id})")
            await asyncio.sleep(30)
            await database_sync_to_async(self.game_room.refresh_from_db)()
            current_turn_index = self.game_room.current_turn
            all_players = await database_sync_to_async(lambda: list(
                self.game_room.players.select_related('user').order_by('id').all()
            ))()

            if not (0 <= current_turn_index < len(all_players)):
                logger.debug("Invalid turn index. Exiting timer_task.")
                return

            current_player = all_players[current_turn_index]
            if current_player.user.id != player.user.id:
                logger.debug(f"Timer expired but it's no longer {player.user.username}'s turn. Ignoring.")
                return

            if player.is_packed:
                logger.debug(f"Player {player.user.username} already packed, no action needed")
                return
            logger.debug(f"Timer expired for player {player.user.username}, marking as packed")
            player.is_packed = True
            await database_sync_to_async(player.save)(update_fields=['is_packed'])
            await self.send_game_data()
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "player_packed",
                    "player_id": player.user.id,
                    "message": f"Player {player.user.username} packed due to timeout"
                }
            )
            active_players_count = await self.get_active_players_count()
            if active_players_count <= 1:
                remaining_players = await self.get_active_players()
                if remaining_players:
                    logger.debug(f"Only one player left, declaring winner: {remaining_players[0].user.username}")
                    await self.update_game_state("showdown_after_pack")
                    winner = remaining_players[0]
                    await self.save_game_history(winner,'pack',self.game_room.current_pot)
                    await database_sync_to_async(winner.user.save)()
                    self.game_room.current_pot = 0
                    await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            "type": "show_result",
                            "message": f"{winner.user.username} wins the pot as all opponents packed!",
                            "winner_id": winner.user.id,
                            "hand_winner": [],  # No cards shown for winner
                            "hand_loser": [],   # No cards shown for loser
                            "active_players_cards": [
                                {"player_id": p.user.id, "username": p.user.username, "cards": []}
                                for p in remaining_players
                            ]
                        }
                    )
                    await self.send_game_data() 
                    logger.debug("Scheduling new round after showdown_after_pack with 10 seconds delay")
                    asyncio.create_task(self.delayed_restart_round())
                else:
                    await self.update_game_state("waiting")
                    await self.send(text_data=json.dumps({'message': 'No active players remain!'}))
            else:
                await self.send_game_data()
                await self.next_turn(active_players_count)
                await self.send_game_data()
        except asyncio.CancelledError:
            logger.debug(f"Timer for player {player.user.username} cancelled")
            raise
        except Exception as e:
            logger.error(f"Error in timer_task for player {player.user.username}: {e}")

    async def cancel_timer(self):
        task = TURN_TIMERS.get(self.game_room_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.debug("Timer successfully canceled.")
        TURN_TIMERS.pop(self.game_room_id, None)



    async def timer_start(self, event):
        """Handle timer start event."""
        logger.debug(f"Sending timer_start to client for player {event['player_id']}")
        await self.send(text_data=json.dumps({
            "type": "timer_start",
            "player_id": event["player_id"],
            "duration": event["duration"]
        }))

    async def player_packed(self, event):
        """Handle player packed event."""
        logger.debug(f"Sending player_packed to client for player {event['player_id']}")
        await self.send(text_data=json.dumps({
            "type": "player_packed",
            "player_id": event["player_id"],
            "message": event["message"]
        }))
    
    @database_sync_to_async
    def get_player_user_id(self, player):
    #Fetch the player user id.
        return player.user.id
    


        # In send_game_data
    
    async def send_game_data(self):
        import time
        start_time = time.time()
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
            "table_limit":self.game_room.table_limit,
            "pot": self.game_room.current_pot,
            "current_turn": self.game_room.current_turn,
            "status": self.game_room.is_active,
            "round_status": self.game_room.round_status,  # Include the round status here
            "sent_at": time.time(),
        }
        # logger.debug(json.dumps(game_data, indent=2))
        # Include hand_cards only during showdown
        if self.game_room.round_status == "showdown":
            for player_data in game_data["players"]:
                # Fetch the player's hand_cards from the database
                player_obj = await self.get_player_by_id(player_data["id"])
                if player_obj and not player_obj.is_spectator and not player_obj.is_packed:
                    player_data["cards"] = player_obj.hand_cards
                else:
                    player_data["cards"] = []
        # Send data to WebSocket group
        await self.channel_layer.group_send(
            self.room_group_name,
            {"type": "game_update", "data": game_data,
             }
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
                "is_spectator": player.is_spectator,
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
        
        if player_count < 2:
            logger.debug("Not enough players to start the game.")
            await self._reset_is_active()
            return
            
        #Calulate the total limit for the game room
        active_players = await self.get_active_players()
        player_balance = []
        for player in active_players:
            user_coins = await database_sync_to_async(lambda:player.user.coins)()
            player_balance.append(user_coins)
            logger.debug(f"Player {player.user.username} coins: {user_coins}") 
        
        table_limit = min(player_balance) * player_count if player_balance else 0
        logger.debug(f"Calculated total_pot: Min({player_balance}) * {player_count} = {table_limit}")
        
        self.game_room.table_limit = table_limit
        await database_sync_to_async(self.game_room.save)(update_fields =['table_limit'])
        logger.debug(f"Set table_limit to {self.game_room.table_limit}")

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
        
        #Check if the table_limit is reached after boot
        await database_sync_to_async(self.game_room.refresh_from_db)()
        if self.game_room.current_pot >= self.game_room.table_limit:
            logger.debug(f"Current pot {self.game_room.current_pot} reached total pot {self.game_room.table_limit}, initiating showdown")
            await self.inititate_showdown_table_limit()
            return
        # Step 5: Finalize game start
        await self.update_game_state("betting")
        logger.debug(f"Starting game with pot = {self.game_room.current_pot}")
        self.game_room.current_turn = 0
        await database_sync_to_async(self.game_room.save)(update_fields=['current_turn'])
        await self.send_game_data()
        await self.start_current_turn_timer()  # Start the timer for the first player


    async def inititate_showdown_table_limit(self):
        logger.debug(f"Initiating showdown: current_pot = {self.game_room.current_pot}, total_pot = {self.game_room.table_limit}")
        await self.update_game_state("showdown")
        active_players = await self.get_active_players()
        if len(active_players) < 2:
            logger.warning("Showdown initiated with fewer than 2 active players")
            if active_players:
                winner = active_players[0]
                logger.debug(f"Single player remains: {winner.user.username} wins by default")
                await self.declare_winner(winner)
            else:
                logger.warning("No active players remain, ending game without winner")
                await self.update_game_state("waiting")
                await self.send(text_data=json.dumps({'message': 'No active players remain!'}))
            return

        # Compare hands of all active players
        winner = active_players[0]
        for player in active_players[1:]:
            winner = await self.compare_hands(winner, player)
        loser_ids = [p.user.id for p in active_players if p.user.id != winner.user.id]

        logger.debug(f"Showdown winner: {winner.user.username}")
        await self.save_game_history(winner, 'show', self.game_room.current_pot)
        winner.user.coins += self.game_room.current_pot
        await database_sync_to_async(winner.user.save)()
        self.game_room.current_pot = 0
        await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])

        # Send show_result with all active players' cards
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "show_result",
                "message": f"Showdown: {winner.user.username} wins the pot!",
                "winner_id": winner.user.id,
                "hand_winner": winner.hand_cards,
                "hand_loser": [],  # Not used for multi-player showdown
                "active_players_cards": [
                    {"player_id": p.user.id, "username": p.user.username, "cards": p.hand_cards}
                    for p in active_players
                ]
            }
        )
        await self.send_game_data()
        logger.debug("Scheduling new round after showdown with 10-second delay")
        asyncio.create_task(self.delayed_restart_round())
    
    async def start_current_turn_timer(self):
        await database_sync_to_async(self.game_room.refresh_from_db)()
        current_index = self.game_room.current_turn

        all_players = await database_sync_to_async(lambda: list(
            self.game_room.players.select_related('user').order_by('id').all()
        ))()

        if 0 <= current_index < len(all_players):
            candidate = all_players[current_index]
            if not candidate.is_packed and not candidate.is_spectator:
                await self.start_timer(candidate)
                logger.debug(f"Started timer for player at index {current_index} - {candidate.user.username}")


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
            
            # Prevent spectators from performing actions
            if player.is_spectator:
                await self.send(text_data=json.dumps({'error': 'Spectators cannot perform actions!'}))
                return
            # Prevent packed players from performing actions
            if player.is_packed and action in ['place_bet', 'place_double_bet', 'sideshow', 'show']:
                await self.send(text_data=json.dumps({'error': 'Packed players cannot perform this action!'}))
                return
            
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
        boot_amount = 100
        if not game_room:
            raise ValueError("Invalid game room")
        if user.coins < boot_amount:
            logger.debug(f"Player {user.username} has insufficient coins to join the game.")
            raise ValueError("Insufficient coins to join the game.Minimum is 100 coins required.")
        return Player.objects.create(user=user, table=game_room)

    @database_sync_to_async
    def remove_player(self, player, game_room):
        """Removes a player from the game room."""
        player.delete()

    async def get_active_players(self):
        from .models import Player
        return await database_sync_to_async(lambda: list(Player.objects.filter(
            table=self.game_room, is_packed=False,is_spectator=False
        ).select_related('user')))()

    @database_sync_to_async
    def get_active_players_count(self):
        """Gets the count of players who haven't packed and aren't spectators."""
        count = self.game_room.players.filter(is_packed=False, is_spectator=False).count()
        logger.debug(f"Active players count: {count}")
        return count

    @database_sync_to_async
    def update_game_state(self, state):

        """Updates the game state."""
        self.game_room.round_status = state
        self.game_room.save()
        logger.debug(f"Game state updated to {self.game_room.round_status}")

    async def handle_bet(self, player_id, amount):
        logger.debug(f"Handling bet for player ID: {player_id}, amount: {amount}")
        await self.cancel_timer() # Cancel timer on action
        player = await self.get_player_by_id(player_id)
        if not player:
            logger.error(f"Player {player_id} not found")
            await self.send(text_data=json.dumps({'error': 'Player not found!'}))
            return
        if player.is_packed:
            logger.debug(f"Player {player.user.username} is packed and cannot place a bet")
            await self.send(text_data=json.dumps({'error': 'Packed players cannot place bets!'}))
            return

        # Refresh game room state
        await database_sync_to_async(self.game_room.refresh_from_db)()
        current_turn_index = self.game_room.current_turn
        all_players = await database_sync_to_async(lambda: list(
            self.game_room.players.select_related('user').order_by('id').all()
        ))()

        if not all_players:
            logger.error("No players available")
            await self.send(text_data=json.dumps({'error': 'No players available!'}))
            return

        # Find previous active player
        prev_player = None
        prev_index = (current_turn_index - 1 + len(all_players)) % len(all_players)
        searched = 0
        while searched < len(all_players):
            candidate = all_players[prev_index]
            if not candidate.is_packed and not candidate.is_spectator:
                prev_player = candidate
                break
            prev_index = (prev_index - 1 + len(all_players)) % len(all_players)
            searched += 1

        # Calculate minimum bet
        min_bet = prev_player.current_bet if prev_player else 1
        if player.is_blind:
            min_bet = prev_player.current_bet if prev_player.is_blind else prev_player.current_bet // 2 if prev_player else 1
        else:
            min_bet = prev_player.current_bet * 2 if prev_player and prev_player.is_blind else prev_player.current_bet if prev_player else 1

        if amount < min_bet:
            await self.send(text_data=json.dumps({'error': f'Bet must be at least {min_bet}'}))
            return

        user_coins = await database_sync_to_async(lambda: player.user.coins)()
        if user_coins >= amount:
            logger.debug(f"Player {player.user.username} placing bet: {amount}")
            await self.update_player_coins(player, -amount)
            await self.update_game_room_pot(amount, context='handle_bet')
            await self.save_current_bet(player, amount)
            await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])
            #Check if table limit is reached.
            await database_sync_to_async(self.game_room.refresh_from_db)()
            if self.game_room.current_pot >= self.game_room.table_limit:
                logger.debug(f"Current pot {self.game_room.current_pot} reached total pot {self.game_room.table_limit}, initiating showdown")
                await self.inititate_showdown_table_limit()
                return

            active_players_count = await self.get_active_players_count()
            await self.next_turn(active_players_count)
            await self.update_game_state("betting")
            if active_players_count <= 1:
                await self.declare_winner()
            else:
                await self.send_game_data()
        else:
            logger.debug(f"Insufficient balance for {player.user.username}: {user_coins} < {amount}")
            await self.send(text_data=json.dumps({'message': 'Insufficent Funds for Bet!'}))

    async def handle_double_bet(self, player_id, amount):
        logger.debug(f"Handling double bet for player ID: {player_id}, amount: {amount}")
        await self.cancel_timer() # Cancel timer on action

        player = await self.get_player_by_id(player_id)
        if not player:
            logger.error(f"Player {player_id} not found")
            await self.send(text_data=json.dumps({'error': 'Player not found!'}))
            return
        if player.is_packed:
            logger.debug(f"Player {player.user.username} is packed and cannot place a double bet")
            await self.send(text_data=json.dumps({'error': 'Packed players cannot place bets!'}))
            return

        # Refresh game room state
        await database_sync_to_async(self.game_room.refresh_from_db)()
        current_turn_index = self.game_room.current_turn
        all_players = await database_sync_to_async(lambda: list(
            self.game_room.players.select_related('user').order_by('id').all()
        ))()

        if not all_players:
            logger.error("No players available")
            await self.send(text_data=json.dumps({'error': 'No players available!'}))
            return

        # Find previous active player
        prev_player = None
        prev_index = (current_turn_index - 1 + len(all_players)) % len(all_players)
        searched = 0
        while searched < len(all_players):
            candidate = all_players[prev_index]
            if not candidate.is_packed and not candidate.is_spectator:
                prev_player = candidate
                break
            prev_index = (prev_index - 1 + len(all_players)) % len(all_players)
            searched += 1

        # Calculate expected double bet amount
        double_bet = prev_player.current_bet * 2 if prev_player else 2
        if player.is_blind:
            double_bet = prev_player.current_bet * 2 if prev_player.is_blind else prev_player.current_bet if prev_player else 2
        else:
            double_bet = prev_player.current_bet * 4 if prev_player and prev_player.is_blind else prev_player.current_bet * 2 if prev_player else 2
        if amount != double_bet:
            await self.send(text_data=json.dumps({'error': f'Double bet must be {double_bet}'}))
            return
        
        user_coins = await database_sync_to_async(lambda: player.user.coins)()
        if user_coins >= amount:
            await self.update_player_coins(player, -amount)
            await self.update_game_room_pot(amount, context='handle_double_bet')
            await self.save_current_bet(player, amount)
            await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])

            # Check if total_pot is reached
            await database_sync_to_async(self.game_room.refresh_from_db)()
            if self.game_room.current_pot >= self.game_room.table_limit:
                logger.debug(f"Current pot {self.game_room.current_pot} reached total pot {self.game_room.table_limit}, initiating showdown")
                await self.inititate_showdown_table_limit()
                return
            
            active_players_count = await self.get_active_players_count()
            await self.next_turn(active_players_count)
            await self.update_game_state("betting")
            await self.send_game_data()
        else:
            logger.debug(f"Insufficient balance for {player.user.username}: {user_coins} < {amount}")
            await self.send(text_data=json.dumps({'message': 'Insufficent Funds for double bet!'}))    

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
        logger.debug(f"Handling pack for player ID: {player_id}")
        await self.cancel_timer() # Cancel timer on action
        await database_sync_to_async(self.game_room.refresh_from_db)()
        # Fetch player and verify
        player = await self.get_player_by_id(player_id)
        if not player:
            logger.error(f"Player {player_id} not found")
            await self.send(text_data=json.dumps({'error': 'Player not found!'}))
            return
        
        if player.is_packed:
            logger.debug(f"Player {player.user.username} is already packed, ignoring action")
            await self.send(text_data=json.dumps({'message': 'You are already packed!'}))
            return
        
        if self.game_room.round_status != 'betting':
            logger.debug(f"Ignoring pack for {player.user.username} since game state is {self.game_room.round_status}")
            await self.send(text_data=json.dumps({'message': 'Game has not started !'}))
            return

        # Refresh game room state to ensure latest pot and turn
        
        current_pot = self.game_room.current_pot if self.game_room.current_pot is not None else 0
        logger.debug(f"Before packing: pot = {current_pot}, player = {player.user.username}")

        # Mark player as packed
        player.is_packed = True
        await database_sync_to_async(player.save)(update_fields=['is_packed'])
        logger.debug(f"Player {player.user.username} marked as packed")
        await self.send_game_data()

        # Get active players count
        active_players_count = await self.get_active_players_count()
        logger.debug(f"Active players after pack: {active_players_count}")

        # Check if game should end
        if active_players_count <= 1:
            remaining_players = await self.get_active_players()
            if remaining_players:
                logger.debug(f"Only one player left, declaring winner: {remaining_players[0].user.username}")
                await self.update_game_state("showdown_after_pack")
                winner = remaining_players[0]
                await self.save_game_history(winner,'pack',self.game_room.current_pot)
                await database_sync_to_async(winner.user.save)()
                self.game_room.current_pot = 0
                await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "show_result",
                        "message": f"{winner.user.username} wins the pot as all opponents packed!",
                        "winner_id": winner.user.id,
                        "hand_winner": [],  # No cards shown for winner
                        "hand_loser": [],   # No cards shown for loser
                        "active_players_cards": [
                            {"player_id": p.user.id, "username": p.user.username, "cards": []}
                            for p in remaining_players
                        ]
                    }
                )
                await self.send_game_data() 
                logger.debug("Scheduling new round after showdown_after_pack with 10 seconds delay")
                asyncio.create_task(self.delayed_restart_round())
            else:
                logger.warning("No active players remain, ending game without winner")
                await self.update_game_state("waiting")
                await self.send(text_data=json.dumps({'message': 'No active players remain!'}))
            return

        # Advance turn using next_turn
        await self.next_turn(active_players_count)
        await self.update_game_state("betting")

        # Verify pot integrity
        await database_sync_to_async(self.game_room.refresh_from_db)()
        if self.game_room.current_pot != current_pot:
            logger.warning(f"Pot changed from {current_pot} to {self.game_room.current_pot} during pack, restoring")
            self.game_room.current_pot = current_pot
            await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])

        logger.debug(f"After packing: pot = {self.game_room.current_pot}, current_turn = {self.game_room.current_turn}")
        await self.send_game_data()

    async def next_turn(self, active_players_count):
        logger.debug(f"Entering next_turn with active_players_count: {active_players_count}")
        await self.cancel_timer() # Cancel existing timer
        logger.debug("Existing timer canceled")

        if active_players_count <= 1:
            logger.debug("Only one or no active player remains, checking for winner")
            remaining_players = await self.get_active_players()
            if remaining_players:
                await self.declare_winner(remaining_players[0])
            return

        try:
            logger.debug("Refreshing game room state for next turn")
            await database_sync_to_async(self.game_room.refresh_from_db)()

            all_players = await database_sync_to_async(lambda: list(
                self.game_room.players.select_related('user').order_by('id').all()
            ))()

            # Get current player list for turn rotation
            current_index = self.game_room.current_turn
            total_players = len(all_players)
            logger.debug(f"Current turn index: {current_index}, total players: {total_players}")

            next_index = (current_index + 1) % total_players
            searched = 0

            # Find next valid player
            while searched < total_players:
                candidate = all_players[next_index]
                if not candidate.is_packed and not candidate.is_spectator:
                    self.game_room.current_turn = next_index
                    await database_sync_to_async(self.game_room.save)(update_fields=["current_turn"])
                    logger.debug(f"Next turn set to index {next_index} - player: {candidate.user.username}")
                    await self.start_timer(candidate)  # Start timer for the next player
                    await self.send_game_data()
                    return
                next_index = (next_index + 1) % total_players
                searched += 1

            logger.warning("No valid player found for the next turn. Triggering winner declaration.")
            active_players = await self.get_active_players()
            if len(active_players) == 1:
                await self.declare_winner(active_players[0])
            else:
                await self.send(text_data=json.dumps({"error": "Turn could not be assigned."}))

        except Exception as e:
            logger.error(f"Error in next_turn: {str(e)}")
            await self.send(text_data=json.dumps({"error": f"Error advancing turn: {str(e)}"}))


    async def handle_sideshow(self, player_id, opponent_id, bet_amount):
        from datetime import datetime, timezone
        await self.send_game_data()
        from django.core.cache import cache
        player = await self.get_player_by_id(player_id)
        opponent = await self.get_player_by_id(opponent_id)
        if not player or not opponent:
            await self.send(text_data=json.dumps({'error': 'Player or opponent not found!'}))
            return
        if player.is_packed:
            await self.send(text_data=json.dumps({'error': 'Packed players cannot request a sideshow!'}))
            return

        await database_sync_to_async(self.game_room.refresh_from_db)()
        all_players = await database_sync_to_async(lambda: list(
            self.game_room.players.select_related('user').order_by('id').all()
        ))()
        current_turn_index = self.game_room.current_turn

        # Check if opponent is the previous active player
        prev_player = None
        prev_index = (current_turn_index - 1 + len(all_players)) % len(all_players)
        searched = 0
        while searched < len(all_players):
            candidate = all_players[prev_index]
            if not candidate.is_packed and not candidate.is_spectator:
                prev_player = candidate
                break
            prev_index = (prev_index - 1 + len(all_players)) % len(all_players)
            searched += 1

        if not prev_player or prev_player.user.id != opponent.user.id:
            await self.send(text_data=json.dumps({'error': 'Sideshow can only be requested with the previous player!'}))
            return

        # Check bet amount and deduct
        if bet_amount is not None:
            if player.user.coins < bet_amount:
                await self.send(text_data=json.dumps({'message': 'Insufficent funds for sideshow!'}))
                return
            await self.update_player_coins(player, -bet_amount)
            await self.save_current_bet(player, opponent.current_bet)
            await self.update_game_room_pot(bet_amount, context="handle_sideshow")
            logger.debug(f"Player {player.user.username} bet {bet_amount} to match {opponent.user.username}")
        
            #Check if the table limit is reached.
            await database_sync_to_async(self.game_room.refresh_from_db)()
            if self.game_room.current_pot >= self.game_room.table_limit:
                logger.debug(f"Current pot {self.game_room.current_pot} reached total pot {self.game_room.table_limit}, initiating showdown")
                await self.inititate_showdown_table_limit()
                return
            
        if player.current_bet < opponent.current_bet:
            await self.send(text_data=json.dumps({'error': 'You must match the previous bet for a sideshow!'}))
            return
        if self.game_room.round_status != 'betting':
            await self.send(text_data=json.dumps({'error': 'Sideshow can only be requested during betting!'}))
            return
        if player.is_blind or opponent.is_blind:
            await self.send(text_data=json.dumps({'error': 'Sideshow is only available between seen players!'}))
            return

        # Store sideshow request in cache
        sideshow_key = f"sideshow_{self.game_room_id}_{player_id}_{opponent_id}"
        cache.set(sideshow_key, {'player_id': str(player_id), 'opponent_id': str(opponent_id), 'accepted': None}, timeout=30)
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

        await self.send(text_data=json.dumps({'message': f'Sideshow request sent to {opponent.user.username}. Awaiting response...'}))
        start_time = TURN_START_TIMES.get(self.game_room_id)
        timeout = 30  # default
        if start_time:
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            remaining = max(0, 30 - elapsed)
            timeout = int(remaining)

        for _ in range(timeout * 2):
            request = cache.get(sideshow_key)
            if request and request['accepted'] is not None:
                break
            await asyncio.sleep(0.5)

        request = cache.get(sideshow_key) or {'accepted': None}
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
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "sideshow_result",
                    "message": f"Sideshow declined by {opponent.user.username}",
                    "packed_player": None
                }
            )
        cache.delete(sideshow_key)

        if active_players_count > 1:
            logger.debug("Advancing turn after sideshow")
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
        cache.set(sideshow_key, request, timeout=30)  # Update cache with response
        logger.debug(f"Sideshow response from {opponent.user.username}: {'Accepted' if accept else 'Declined'}")
    
    async def sideshow_result(self, event):
        """Broadcast sideshow result to all players."""
        logger.debug(f"Sideshow request event: {event}")
        await self.send(text_data=json.dumps(event))

    
    async def handle_show(self, player_id):
        import time
        start_time = time.time()
        logger.debug(f"Time of when the handle show is executed : {start_time}")
        await self.cancel_timer() # Cancel timer on action
        await database_sync_to_async(self.game_room.refresh_from_db)()
        logger.debug(f"Initial current_pot in handle_show: {self.game_room.current_pot}")
        await self.update_game_state("showdown")
        player = await self.get_player_by_id(player_id)
        if not player:
            logger.error(f"Player {player_id} not found")
            await self.send(text_data=json.dumps({'error': 'Player not found!'}))
            return

        active_players = await self.get_active_players()
        if len(active_players) != 2:
            await self.send(text_data=json.dumps({'error': 'Show can only be called with exactly 2 players!'}))
            return
        if self.game_room.round_status != 'showdown':
            logger.debug(f"Invalid game state: {self.game_room.round_status}")
            await self.send(text_data=json.dumps({'error': 'Show can only be called during showdown!'}))
            return

        all_players = await database_sync_to_async(lambda: list(
            self.game_room.players.select_related('user').order_by('id').all()
        ))()
        current_turn_index = self.game_room.current_turn

        # Find previous active player
        prev_player = None
        prev_index = (current_turn_index - 1 + len(all_players)) % len(all_players)
        searched = 0
        while searched < len(all_players):
            candidate = all_players[prev_index]
            if not candidate.is_packed and not candidate.is_spectator:
                prev_player = candidate
                break
            prev_index = (prev_index - 1 + len(all_players)) % len(all_players)
            searched += 1

        amount = (prev_player.current_bet
                if player.is_blind or (not player.is_blind and not prev_player.is_blind)
                else 2 * prev_player.current_bet if prev_player else 1)
        user_coins = await database_sync_to_async(lambda: player.user.coins)()
        if user_coins < amount:
            logger.debug(f"Insufficient balance for {player.user.username}: {user_coins} < {amount}")
            await self.send(text_data=json.dumps({'message': 'Insufficent funds for show!'}))
            return

        if user_coins >= amount:
            logger.debug(f"Deducting stake {amount} from {player.user.username}")
            await database_sync_to_async(self.game_room.refresh_from_db)()
            await self.update_player_coins(player, -amount)
            await self.update_game_room_pot(amount, context='handle_show')
            await self.save_current_bet(player, amount)

            # Check if total_pot is reached
            await database_sync_to_async(self.game_room.refresh_from_db)()
            if self.game_room.current_pot >= self.game_room.table_limit:
                logger.debug(f"Current pot {self.game_room.current_pot} reached total pot {self.game_room.table_limit}, initiating showdown")
                await self.inititate_showdown_table_limit()
                return  

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
            await self.send(text_data=json.dumps({'error': 'Error: No valid opponent for showdown!'}))
            return

        winner = await self.compare_hands(player, opponent)
        loser = opponent if winner.user.id == player.user.id else player
        logger.debug(f"Winner: {winner.user.username}, Loser: {loser.user.username}")

        await self.save_game_history(winner, 'show', self.game_room.current_pot)

        logger.debug(f"Awarding pot of {self.game_room.current_pot} to {winner.user.username}")
        winner.user.coins += self.game_room.current_pot
        await database_sync_to_async(winner.user.save)()
        self.game_room.current_pot = 0
        await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])
        
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "show_result",
                "message": f'Show: {winner.user.username} wins the pot!',
                "winner_id": winner.user.id,
                "hand_winner": winner.hand_cards,
                "hand_loser": opponent.hand_cards,
                "active_players_cards": [
                    {"player_id": p.user.id, "username": p.user.username, "cards": p.hand_cards}
                    for p in active_players
                ]
            }
        )
        logger.debug("Sent show_result message")
        await self.send_game_data()
        end_time = time.time()
        logger.debug(f" Time of ending show function : {end_time} seconds")
        logger.debug(f"[SHOW] Time to process show: {end_time - start_time:.4f} seconds")
        logger.debug("Scheduling new round after showdown with 10-second delay")
        # Detach restart_round to prevent blocking
        asyncio.create_task(self.delayed_restart_round())
        
    async def delayed_restart_round(self):
        """Delays the restart of the round by 10 seconds."""
        logger.debug("Waiting 10 seconds before restarting round...")
        await asyncio.sleep(10)  # Wait 10 seconds to allow result display
        self.game_room.is_active = False
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
        
        # Check coins and remove players with insufficient balance
        boot_amount = 100
        players_to_remove = []
        for player in all_players:
            user_coins = await database_sync_to_async(lambda: player.user.coins)()
            if user_coins < boot_amount:
                logger.debug(f"Player {player.user.username} has insufficient coins: {user_coins} < {boot_amount}. Marking for removal.")
                players_to_remove.append(player)

        # Remove players with insufficient coins
        for player in players_to_remove:
            await self.remove_player(player, self.game_room)
            logger.debug(f"Removed player {player.user.username} from game room {self.game_room_id}")
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "player_disconnected",
                    "player_id": await database_sync_to_async(lambda: player.user.id)(),
                    "message": f"Player {player.user.username} removed due to insufficient coins (less than {boot_amount})."
                }
            )
        
        # Refresh player list after removals
        all_players = await database_sync_to_async(lambda: list(self.game_room.players.all()))()
        player_count = len(all_players)
        logger.debug(f"Player count after coin check: {player_count}")
        if player_count < 2:
            logger.debug("Not enough players after coin check to restart the round.")
            await self.update_game_state("waiting")
            await self.send(text_data=json.dumps({
                'message': 'Not enough players to continue, waiting for more players!'
            }))
            await self._reset_is_active()
            return

        @database_sync_to_async
        def reset_players(players, boot_amount):
            with transaction.atomic():
                for player in players:
                    player.is_packed = False
                    player.is_blind = True
                    player.is_spectator = False
                    player.current_bet = 0
                    player.hand_cards = []
                    player.save()
                    logger.debug(f"Reset player {player.user.username}, hand_cards: {player.hand_cards}")

        
        await reset_players(all_players, boot_amount=100)

        # Set is_active = False for the new round
        self.game_room.is_active = False
        await database_sync_to_async(self.game_room.save)(update_fields=['is_active'])
        # Additional logging to verify hand_cards are cleared after reset
        @database_sync_to_async
        def log_player_cards(players):
            for player in players:
                player.refresh_from_db() 
                logger.debug(f"After reset - Player {player.user.username} -is_packed : {player.is_packed} - hand_cards: {player.hand_cards}")

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
        self.game_room.is_active = True
        await database_sync_to_async(self.game_room.save)(update_fields=['is_active'])
        logger.debug(f"New round: is_active = {self.game_room.is_active}, round_status = {self.game_room.round_status}")
        
        await self.send_game_data()
        await self.start_current_turn_timer()


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
        asyncio.create_task(self.delayed_restart_round())