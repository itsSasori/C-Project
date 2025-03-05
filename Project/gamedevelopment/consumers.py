# consumers.py
import asyncio
from datetime import datetime
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

logger = logging.getLogger('gamedevelopment')

#Create your consumers.py here:
class GameConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.game_room_id = self.scope['url_route']['kwargs']['game_room_id']
        self.room_group_name = f'game_{self.game_room_id}'

        self.game_room = await self.get_game_room(self.game_room_id)
        logger.debug(self.game_room)

        if not self.game_room:
            await self.send(text_data=json.dumps({"error": "Game room not found"}))
            await self.close()
            return

        self.player = await self.get_player(self.scope["user"], self.game_room)
        
        await database_sync_to_async(self.game_room.refresh_from_db)()
        logger.debug(f"Connected to game room {self.game_room_id}: pot = {self.game_room.current_pot}, round_status = {self.game_room.round_status}")
        if not self.player:
            self.player = await self.create_player(self.scope["user"], self.game_room)
            logger.debug(self.player)
        
        # Rejoin the game group after a reload
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()
        await self.send_game_data()


        # Only start the game if it hasn’t started yet and enough players are present
        player_count = await self.get_active_players_count()
        logger.debug(f"Player count: {player_count}")
        if player_count >= 2 and self.game_room.round_status not in ['Betting', 'Distribution']:
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
                "cards": player.hand_cards if player.user.id == self.scope["user"].id else [],
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
        self.game_room.is_active = False
        await database_sync_to_async(self.game_room.save)(update_fields=['current_turn', 'is_active'])
        await self.send_game_data()

# Helper method to check and lock game start atomically
    @database_sync_to_async
    def _check_and_lock_game_start(self):
            with transaction.atomic():
                self.game_room.refresh_from_db()
                if self.game_room.is_active or self.game_room.round_status in ['Betting', 'Distribution']:
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
        players = await self.get_players()  # Get the players in the game
        if len(players) < 2:
            logger.debug("Not enough players to distribute cards.")
            return 
        # Create a deck of cards
        suits = ['♠', '♥', '♦', '♣']
        ranks = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
        deck = [rank + suit for suit in suits for rank in ranks]
        random.shuffle(deck)

        cards_per_player = 3  # Number of cards per player in Teen Patti
        for player in players:
            # Give 3 cards to each player
            player_cards = deck[:cards_per_player]
            deck = deck[cards_per_player:] 
             # Assign the card to the player (you can store this in a player attribute or model)
            await self.assign_card_to_player(player['id'], player_cards)
            logger.debug(f"Player {player['username']} got {player_cards}")



    async def assign_card_to_player(self, player_id, cards):
        # """Assigns a card to a player"""
        player = await self.get_player_by_id(player_id)
        if player:
            player.hand_cards.clear()  # Clear previous hand before assigning new cards
            player.hand_cards.extend(cards)  # Assign all three cards at once
            await self.save_player(player)
        # logger.debug(f"Card {card} assigned to player {player.user.username} to player {player_id}")
    
    
    @database_sync_to_async
    def save_player(self, player):
        player.save()
    

    @database_sync_to_async
    def get_player_by_id(self, player_id):
        from .models import Player
        """Fetch a player by their user's ID."""
        try:
            player = Player.objects.get(user__id=player_id)
            logger.debug(f"Player with ID {player_id} found: {player}")
            return player
        except Player.DoesNotExist:
            logger.error(f"Player with user ID {player_id} does not exist!")
            return None
        
    async def receive(self, text_data):
        from .models import Player
        try:
            text_data_json = json.loads(text_data)
            action = text_data_json.get('action')
            player_id = text_data_json.get('player_id')
            player = await database_sync_to_async(Player.objects.get)(user__id=player_id, table=self.game_room)
            
            if action == 'place_bet':
                await self.handle_bet(player_id, text_data_json.get('amount'))
            elif action == 'place_double_bet':
                await self.handle_double_bet(player_id,text_data_json.get('amount'))
            elif action == 'pack':
                await self.handle_pack(player_id)
            elif action == 'sideshow':
                await self.handle_sideshow(player_id, text_data_json.get('opponent_id'))
            elif action == 'toggle_seen':
                await self.toggle_seen(player)
            

            # Ensure this is called to send the game data to WebSocket after any action
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
            await self.update_game_room_pot(amount)
            await self.save_current_bet(player, amount)
            active_players_count = await self.get_active_players_count()
            await self.update_game_room_turn(active_players_count)
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
            await self.update_game_room_pot(amount)
            await self.save_current_bet(player, amount)
            active_players_count = await self.get_active_players_count()
            await self.update_game_room_turn(active_players_count)
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
    def update_game_room_pot(self, amount):
        with transaction.atomic():
            self.game_room.refresh_from_db()  #Ensure latest state
            if self.game_room.current_pot is None:
                self.game_room.current_pot = 0  # Initialize if None
                logger.debug(f"Initialized current_pot to 0")
            old_pot = self.game_room.current_pot
            self.game_room.current_pot += amount
            logger.debug(f"Pot update: {old_pot} + {amount} = {self.game_room.current_pot}")
            self.game_room.save(update_fields=['current_pot'])
            logger.debug(f"Updated pot to {self.game_room.current_pot}")

    @database_sync_to_async
    def save_current_bet(self, player, amount): 
        with transaction.atomic():
            player.refresh_from_db()  # Get latest current_bet before update
            logger.debug(f"Before update - Current bet for {player.user.username}: {player.current_bet}")
            player.current_bet = amount  # Accumulate bets, not overwrite
            player.save(update_fields=['current_bet'])
            player.refresh_from_db()  # Ensure latest value
            logger.debug(f"After update - Current bet for {player.user.username}: {player.current_bet}")

    @database_sync_to_async
    def update_game_room_turn(self, active_players_count):
        self.game_room.current_turn = (self.game_room.current_turn + 1) % active_players_count
        self.game_room.save(update_fields=['current_turn'])
        logger.debug(f"Turn updated to {self.game_room.current_turn}")


    async def handle_pack(self, player_id):
        player = await self.get_player_by_id(player_id)
        if not player:
            return
        
        # Refresh game room state to ensure we have the latest pot value
        await database_sync_to_async(self.game_room.refresh_from_db)()
        current_pot = self.game_room.current_pot
        logger.debug(f"Before packing: pot = {current_pot}, player = {player.user.username}")
        player.is_packed = True
        await database_sync_to_async(player.save)()
        
        remaining_players = await self.get_active_players()
        active_players_count = len(remaining_players)
        logger.debug(f"Active players after pack: {active_players_count}")
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
        await self.send_game_data()

    async def next_turn(self, active_players_count):
        players = await self.get_active_players()
        current_turn = self.game_room.current_turn
        
        if current_turn >= active_players_count or current_turn < 0:
            current_turn = 0
        
        # Always advance to the next active player
        for i in range(active_players_count):
            next_index = (current_turn + i) % active_players_count
            if not players[next_index].is_packed:
                self.game_room.current_turn = next_index
                await database_sync_to_async(self.game_room.save)()
                logger.debug(f"Turn advanced from {current_turn} to player at index {next_index}, ID: {players[next_index].user.id}")
                break
        else:
            # Only declare winner if truly 1 active player remains
            remaining_players = await self.get_active_players()
            if len(remaining_players) == 1:
                logger.debug(f"One active player remains: {remaining_players[0].user.id}")
                await self.declare_winner(remaining_players[0])
            elif len(remaining_players) == 0:
                logger.error("No active players found, ending game")
                await self.send(text_data=json.dumps({"error": "No active players left"}))
                return
            else:
                logger.error(f"Unexpected state: {len(remaining_players)} active players, but no turn found")
                return
        
        await self.send_game_data()

    async def handle_sideshow(self, player_id, opponent_id):
        player = await self.get_player_by_id(player_id)
        opponent = await self.get_player_by_id(opponent_id)
        
        winner = await self.compare_hands(player, opponent)
        await self.declare_winner(winner)
        
        await self.send_game_data()
    
    # async def next_turn(self):
    #     self.game_room.current_turn = (self.game_room.current_turn + 1) % await self.get_active_players_count()
    #     await database_sync_to_async(self.game_room.save)()
    #     await self.send_game_data()

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


    async def declare_winner(self, player):
        if player:
            logger.debug(f"Declaring winner: {player.user.username}, pot before reset: {self.game_room.current_pot}")
            player.user.coins += self.game_room.current_pot
            self.game_room.current_pot = 0
            await database_sync_to_async(player.save)()
            await database_sync_to_async(self.game_room.save)()
            
            await self.send(text_data=json.dumps({
                "message": f"{player.user.username} wins the round!",
                "winner": player.user.id
            }))
            # Start a new round
            await self.start_game()
        else:
            await self.send(text_data=json.dumps({
                "message": "No active players left!",
            }))
    
    async def compare_hands(self, player1, player2):
        hand1_value = await self.calculate_hand_value(player1.hand_cards)
        hand2_value = await self.calculate_hand_value(player2.hand_cards)
        return player1 if hand1_value > hand2_value else player2
    
    async def calculate_hand_value(self, hand_cards):
        card_values = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
                       "J": 11, "Q": 12, "K": 13, "A": 14}
        return sum(card_values[card[:-1]] for card in hand_cards)
