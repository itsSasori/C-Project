# consumers.py
import asyncio
from datetime import datetime
import string
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

# List of realistic bot names
BOT_NAMES = [
    'Alex', 'Jordan', 'Taylor', 'Morgan', 'Casey', 'Riley', 'Jamie', 'Quinn',
    'Avery', 'Cameron', 'Parker', 'Reese', 'Skyler', 'Dakota', 'Finley', 'Emerson'
]

# Global timer store by game_room_id
TURN_TIMERS = {}
TURN_START_TIMES = {}
RECONNECT_TIMERS = {}
TIMER_TRACKER = {}

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
        
        is_reconnecting = False
        try:
            self.player = await self.get_player(self.scope["user"], self.game_room)
            if not self.player:
                self.player = await self.create_player(self.scope["user"], self.game_room)
                logger.debug(f"Created player: {self.player}")
            else:
                is_reconnecting = True
            await self.accept()
            
            if self.player:
                await self.handle_reconnect(self.player)
        except ValueError as e:
            await self.accept()
            await self.send(text_data=json.dumps({"error": str(e)}))
            await self.close()
            return
        
        await database_sync_to_async(self.game_room.refresh_from_db)()
        logger.debug(f"Connected to game room {self.game_room_id}: pot = {self.game_room.current_pot}, round_status = {self.game_room.round_status}, is_active = {self.game_room.is_active}")        
        
        
            # Set new player as spectator if game is in progress
        if not is_reconnecting and self.game_room.round_status in ['betting', 'distribution']:
            self.player.is_spectator = True  # Use is_spectator 
            await database_sync_to_async(self.player.save)(update_fields=['is_spectator'])
            await self.send(text_data=json.dumps({
                    "type": "spectator_notification",
                    "message": "Game in progress. You are a spectator until the next round starts."
                }))
        
        # join the game group
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.notify_reconnect(self.player)
        await self.send_game_data()

        # Ensure the reconnecting client also sees the current timer
        await self.resend_current_timer()
        
        # Only start the game if it hasn’t started yet and enough players are present
        player_count = await self.get_active_players_count()
        logger.debug(f"Player count: {player_count}")
        if player_count >= 1 and not self.game_room.is_active and self.game_room.round_status == "waiting":            
            logger.debug("Scheduling game start with 5-second delay")
            
            asyncio.create_task(self.delayed_start_game())  # Schedule with delay
        else:
            logger.debug("Not enough players to start the game or game already active")

    async def handle_reconnect(self, player):
        from datetime import datetime, timezone
        """Handle player reconnection.with 10 seconds window"""
        user = await database_sync_to_async(lambda: player.user)()
        logger.debug(f"Checking reconnect for player{user.username} (ID: {user.id})")
        if player.disconnected_at:
            time_since_disconnect = (datetime.now(timezone.utc) - player.disconnected_at).total_seconds()
            logger.debug(f"Time since disconnect: {time_since_disconnect} seconds")
            reconnect_key = f'reconnect_{self.game_room_id}_{player.user.id}'
            task = RECONNECT_TIMERS.get(reconnect_key)

            if time_since_disconnect <= 10:
                player.disconnected_at = None  # Reset disconnect time
                await database_sync_to_async(player.save)(update_fields=['disconnected_at'])
                logger.debug(f"Player {player.user.username} reconnected within 10 seconds, resetting disconnect time")
                
                if task:
                    task.cancel()
                    del RECONNECT_TIMERS[reconnect_key]
                    logger.debug(f"Cancelled reconnect timer for player {player.user.username}")
                RECONNECT_TIMERS.pop(reconnect_key, None)
                # Send reconnect notification to other players
                await self.channel_layer.group_send(
                    self.room_group_name, {
                        "type": "player_reconnected",
                        "player_id": player.user.id,
                        "message": f"{player.user.username} has reconnected."
                    }
                )

                #Checked if reconnected_player is the current turn holder.
                await database_sync_to_async(self.game_room.refresh_from_db)()
                all_players = await database_sync_to_async(lambda: list(
                    self.game_room.players.select_related('user').order_by('id').all()
                ))()
                remaining_time = self.get_remaining_time_for_player(player.user.id)
                if remaining_time > 0:
                    await self.send(text_data=json.dumps({
                        "type": "timer_start",
                        "player_id": player.user.id,
                        "duration": remaining_time
                    }))
            else:
                #Player reconnected too late,mark as packed
                player.disconnected_at = None  # Reset disconnect time
                player.is_spectator = True
                await database_sync_to_async(player.save)(update_fields=['disconnected_at', 'is_spectator'])
                logger.debug(f"Player {player.user.username} reconnected too late, marking as spectator")
                
                if task:
                    task.cancel()
                    del RECONNECT_TIMERS[reconnect_key]
                    logger.debug(f"Cancelled reconnect timer for player {player.user.username}")
                
                await self.send(text_data=json.dumps({
                    "type": "spectator_notification",
                    "message": "You reconnected too late and are now marked as packed. You will be a spectator until the next round starts."
                }))
                await self.channel_layer.group_send(
                    self.room_group_name, {
                        "type": "player_reconnected",
                        "player_id": player.user.id,
                        "message": f"Player {player.user.username} has reconnected and marked as spectator ."
                    }
                )
                #Checked if packed player was the current turn holder.
                await database_sync_to_async(self.game_room.refresh_from_db)()
                
                all_players = await database_sync_to_async(lambda: list(
                    self.game_room.players.select_related('user').order_by('id').all()
                    ))()
                active_players_count = await self.get_active_players_count()
                if  self.game_room.round_status in ['betting'] and (self.game_room.current_turn < len(all_players) and all_players[self.game_room.current_turn].user.id == player.user.id):
                    logger.debug(f"Spectator player {player.user.username} was current turn holder, advancing turn")
                    await self.next_turn(active_players_count)                
        else:
            logger.debug(f"Player {player.user.username} has no disconnect time, treating as normal reconnect")  
            
        # Send current game state to reconnected player
        await self.send_game_data()

        await self.resend_current_timer()

    def get_remaining_time_for_player(self, player_id):
        from datetime import datetime, timezone
        start_time, duration = TIMER_TRACKER.get(player_id, (None, None))
        if not start_time:
            return 0
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        remaining = duration - elapsed
        return max(0, int(remaining))

    async def notify_reconnect(self, player):
        """Notify other players about reconnecting player"""
        await self.channel_layer.group_send(
            self.room_group_name, {
                "type": "player_reconnected",
                "player_id": player.user.id,
                "message": f"{player.user.username} has reconnected."
            }
        )

    async def delayed_start_game(self):
        """Delays the game start by 5 seconds to allow more players to join."""
        # Check if game is already starting or started
        should_proceed = await self._check_and_lock_game_start()
        if not should_proceed:
            logger.debug("Game already starting or in progress, skipping delayed start")
            return

        logger.debug("Waiting 5 seconds before starting game...")
        await asyncio.sleep(5)  # Wait 5 seconds for more players to join
        # Adjust bots based on real player count
        # Adjust bots based on real player count
        if getattr(self.game_room, "is_private", False):
            logger.debug(f"Skipping bot addition: Table {self.game_room.id} is private.")
        else:
            await self.adjust_bots()

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
        from datetime import datetime, timezone
        """Handles player exit in real-time."""

        # Guard against missing game_room or player
        if not self.game_room or not self.player:
            logger.debug("Disconnect called but no active game_room or player")
            return

        user = getattr(self.player, "user", None)
        if not user:
            logger.debug("Disconnect called but player has no linked user")
            return

        player_id = user.id
        username = user.username

        # Refresh latest game state
        await database_sync_to_async(self.game_room.refresh_from_db)()

        # Case 1: Remove immediately after showdown/waiting
        if self.game_room.round_status in ["showdown", "showdown_after_pack", "waiting"]:
            await self.remove_player(self.player, self.game_room)
            logger.debug(f"Player {username} (ID: {player_id}) removed immediately after showdown.")

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "player_disconnected",
                    "player_id": player_id,
                    "message": f"Player {username} has left the table after showdown."
                },
            )
            return

        #  Case 2: Mark disconnected & start reconnect timer
        self.player.disconnected_at = datetime.now(timezone.utc)
        await database_sync_to_async(self.player.save)(update_fields=['disconnected_at'])
        logger.debug(f"Player {username} (ID: {player_id}) disconnected at {self.player.disconnected_at}")

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "player_disconnected",
                "player_id": player_id,
                "message": f"{username} has disconnected. They have 10 seconds to reconnect."
            },
        )

        # Start reconnect timer
        reconnect_key = f'reconnect_{self.game_room_id}_{player_id}'
        logger.debug("Reconnect timer started")
        RECONNECT_TIMERS[reconnect_key] = asyncio.create_task(
            self.reconnect_timer(self.player, self.player.disconnected_at)
        )

        #  Check if player is current turn holder
        await database_sync_to_async(self.game_room.refresh_from_db)()
        all_players = await database_sync_to_async(lambda: list(
            self.game_room.players.select_related('user').order_by('id').all()
        ))()

        if (
            self.game_room.current_turn < len(all_players)
            and all_players[self.game_room.current_turn].user.id == player_id
            and not self.player.is_packed
            and not self.player.is_spectator
        ):
            logger.debug(f"Player {username} is current turn holder, timer continues")
        else:
            logger.debug(f"Player {username} is not current turn holder, cancelling timer")
            
    async def reconnect_timer(self, player,disconnect_time):
        from datetime import datetime, timezone
        """Wait 10 seconds before marking player as packed."""
        try:
            logger.debug(f"Starting reconnect timer for player {player.user.username}")
            # Use a separate task to track the sleep
            async def timeout_task():
                try:
                    await asyncio.sleep(10)
                    logger.debug(f"Reconnect timer completed for {player.user.username}")
                    return True
                except asyncio.CancelledError:
                    logger.debug(f"Timeout task cancelled for {player.user.username}")
                    return False
                except Exception as e:
                    logger.error(f"Error in timeout task for {player.user.username}: {e}")
                    return False

            timeout_task = asyncio.create_task(asyncio.sleep(10))
            try:
                await timeout_task
                logger.debug(f"Reconnect timer expired for {player.user.username}, marking as packed (disconnect_time: {disconnect_time})")
            except asyncio.CancelledError:
                logger.debug(f"Reconnect timer cancelled for player {player.user.username}")
                return
                        
            # Check if player still exists in the database

            player_exists = await database_sync_to_async(lambda: self.game_room.players.filter(id=player.id).exists())()
            
            if not player_exists:
                logger.debug(f"Player {player.user.username} no longer exists in game room, marking as packed")
                return
            #Mark player as packed.
            player.is_packed = True
            try:
                await database_sync_to_async(player.save)(update_fields=['is_packed'])
            except Exception as e:
                logger.error(f"Failed to save packed status for {player.user.username}: {e}")
                return
                
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "player_packed",
                    "player_id": player.user.id,
                    "message": f"Player {player.user.username} packed due to disconnection timeout (player removed)"
                }
            )
            logger.debug(f"Sending updated game data after packing {player.user.username} (player removed)")
            await self.send_game_data()
            
            active_players_count = await self.get_active_players_count()
            if active_players_count <= 1:
                remaining_players = await self.get_active_players()
                if remaining_players:
                    logger.debug(f"Only one player left, declaring winner: {remaining_players[0].user.username}")
                    await self.update_game_state("showdown_after_pack")
                    await self.declare_winner(remaining_players[0])
                    await self.send_game_data()
                    asyncio.create_task(self.delayed_restart_round())
                else:
                    logger.debug("No active players remain after timeout.")
                    await self.update_game_state("waiting")
                    await self.send(text_data=json.dumps({'message': 'No active players remain!'}))
            else:
                # Check if packed player was current turn holder
                await database_sync_to_async(self.game_room.refresh_from_db)()
                all_players = await database_sync_to_async(lambda: list(
                        self.game_room.players.select_related('user').order_by('id').all()
                    ))()
                active_players_count = await self.get_active_players_count()
                if active_players_count <= 1:
                    logger.debug("Only one active player left after packing, declaring winner")
                    await self.update_game_state("showdown_after_pack")
                    await self.declare_winner(remaining_players[0])
                else:
                    if self.game_room.current_turn < len(all_players) and all_players[self.game_room.current_turn].user.id == player.user.id:
                        logger.debug(f"Packed player {player.user.username} was current turn holder, advancing turn")
                        await self.next_turn(active_players_count)
                await self.send_game_data()
        except asyncio.CancelledError:
            logger.debug(f"Reconnect timer cancelled for player {player.user.username}")
            
        await self.resend_current_timer()
            
    async def resend_current_timer(self):
        """Resend the timer_start event with remaining time to sync UI."""
        await database_sync_to_async(self.game_room.refresh_from_db)()
        all_players = await database_sync_to_async(lambda: list(
            self.game_room.players.select_related('user').order_by('id').all()
        ))()
        if self.game_room.round_status in ['betting'] and self.game_room.current_turn < len(all_players):
            current_player = all_players[self.game_room.current_turn]
            if not current_player.is_packed and not current_player.is_spectator:
                remaining_time = self.get_remaining_time_for_player(current_player.user.id)
                if remaining_time > 0:
                    logger.debug(f"Resending timer of current player {current_player.user.username} with {remaining_time}s remaining")
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            "type": "timer_start",
                            "player_id": current_player.user.id,
                            "duration": remaining_time
                        }
                    )

    async def player_reconnected(self, event):
        """Handles a player reconnect event and updates the UI."""
        await self.send(text_data=json.dumps({
            "type": "player_reconnected",
            "player_id": event["player_id"],
            "message": event["message"]
            }))
        
    async def player_disconnected(self, event):
        """Handles a player disconnect event and updates the UI."""
        await self.send_game_data()
        await self.send(text_data=json.dumps({
            "type": "player_disconnected",
            "player_id": event["player_id"],
            "message": event["message"]
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
        TIMER_TRACKER[player.user.id] = (datetime.now(timezone.utc), 30)  # Store start time and duration
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
                TIMER_TRACKER.pop(player.user.id, None)  # Clean up timer tracking
                return

            if player.is_packed:
                logger.debug(f"Player {player.user.username} already packed, no action needed")
                TIMER_TRACKER.pop(player.user.id, None)  # Clean up timer tracking
                return
            logger.debug(f"Timer expired for player {player.user.username}, marking as packed")
            #Clean up timer tracking.
            TIMER_TRACKER.pop(player.user.id, None)
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
                    logger.debug(f"Declaring winner: {winner.user.username}")
                    pot = self.game_room.current_pot
                    logger.debug(f"Awarding pot of {pot} to {winner.user.username}")
                    # Credit coins
                    winner.user.coins += pot
                    await database_sync_to_async(winner.user.save)(update_fields=['coins'])
                    # Reset pot
                    self.game_room.current_pot = 0
                    await self.add_earned_coins(winner.user, pot)

                    await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])
                    # Save history
                    await self.save_game_history(winner, 'win', pot)
                    await self.update_challenge_progress(winner, hand_type=None)

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
        from .models import GameTable
        import time
        start_time = time.time()
        logger.debug(f"[send_game_data] Triggered for game room {self.game_room_id}")
        # 🔹 Step 1: Validate self.game_room
        if not hasattr(self, 'channel_layer') or self.channel_layer is None:
            logger.debug(f"[send_game_data] WebSocket already closed for user {self.scope['user'].username}, skipping send")
            return
    
        if not self.game_room:
            logger.error(f"[send_game_data] self.game_room is None for game_room_id {self.game_room_id}")
            self.game_room = await database_sync_to_async(lambda: GameTable.objects.filter(pk=self.game_room_id).first())()
            if not self.game_room:
                logger.error(f"[send_game_data] Game room {self.game_room_id} does not exist")
                await self.safe_send({"error": "Game room not found"})
                await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
                await self.close(code=4000)
                return

        # 🔹 Step 2: Validate self.player
        if not self.player:
            logger.info(f"[send_game_data] Player {self.scope['user'].username} already removed, skipping player-specific updates")
        else:
            # Refresh self.player to ensure latest state
            try:
                player_id = await database_sync_to_async(lambda: self.player.user.id)()
                self.player = await self.get_player_by_id(player_id)
            except Exception as e:
                logger.warning(f"[send_game_data] Error refreshing player: {e}")
                self.player = None
            

        # Refresh game room state to ensure latest values
        await database_sync_to_async(self.game_room.refresh_from_db)()
        logger.debug(f"[send_game_data] Refreshed game room state: pot={self.game_room.current_pot}, "
                    f"round_status={self.game_room.round_status}, is_active={self.game_room.is_active}")

        
        # Fetch all players with necessary fields in one query
        players = await self.get_players()
        if not players:
            logger.warning("[send_game_data] No players found for game room")
            await self.safe_send({"error": "No players found"})
            # 🔹 NEW: Close the consumer
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
            await self.close(code=4000)
            return

        # Filter active players
        active_players = [
            p for p in players 
            if not p["is_packed"] and not p["is_spectator"] and not p.get("disconnected_at")
        ]
        active_player_ids = [p["id"] for p in active_players]
        logger.debug(f"[send_game_data] Active players: {[p['username'] for p in active_players]}")



        # Construct game data
        game_data = {
            "type": "game_update",
            "game_room_id": self.game_room.id,
            "players": players,
            "active_players": active_player_ids,
            "table_limit": self.game_room.table_limit,
            "pot": self.game_room.current_pot,
            "current_turn": self.game_room.current_turn,
            "status": self.game_room.is_active,
            "round_status": self.game_room.round_status,
            "sent_at": time.time(),
        }

        # Send game data to all clients
        await self.channel_layer.group_send(
            self.room_group_name,
            {"type": "game_update", "data": game_data}
        )
        logger.debug(f"[send_game_data] Game data sent in {time.time() - start_time:.4f} seconds")

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
                "current_bet": player.current_bet,  # Ensure current_bet is included

            
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
        await database_sync_to_async(self.game_room.refresh_from_db)()
        if self.game_room.is_active and self.game_room.round_status in ["betting", "distribution"]:
            logger.debug(f"Game already active in room {self.game_room_id}, skipping start_game")
            return
        
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

        logger.debug(f"Declaring winner: {winner.user.username}")
        pot = self.game_room.current_pot
        logger.debug(f"Awarding pot of {pot} to {winner.user.username}")
        # Credit coins
        winner.user.coins += pot
        await database_sync_to_async(winner.user.save)(update_fields=['coins'])
        # Reset pot
        self.game_room.current_pot = 0
        await self.add_earned_coins(winner.user, pot)

        await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])

        # Save history

        await self.save_game_history(winner, 'win', pot)
        await self.update_challenge_progress(winner, hand_type=winner.hand_type)

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
            if self.game_room.round_status in ['betting', 'distribution'] or self.game_room.is_active:
                return False
            self.game_room.is_active = True
            
            self.game_room.save(update_fields=['is_active', 'round_status'])
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
            logger.error(f"[get_player_by_id] Player with user ID {player_id} does not exist in table {self.game_room.id}!")
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
            player = await database_sync_to_async(
                lambda: Player.objects.select_related('user').get(
                    user__id=player_id, table=self.game_room
                )
            )()
            
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

    async def toggle_seen(self, player):
        """
        Handles a player choosing to reveal their blind cards.
        Ensures the player's cards are cached and the game state updates properly.
        """
        logger.debug(f"[toggle_seen] Starting for player: {player.user.username}, "
                    f"is_blind: {player.is_blind}, hand_cards: {player.hand_cards}")

        # Refresh latest DB state
        await database_sync_to_async(self.game_room.refresh_from_db)()
        await database_sync_to_async(player.refresh_from_db)()

        # 1️⃣ Update player's blind status
        player.is_blind = False
        await database_sync_to_async(player.save)(update_fields=['is_blind'])
        logger.debug(f"[toggle_seen] Updated player.is_blind={player.is_blind}")

        # 2️⃣ Fetch and cache hand cards for this WebSocket session
        cards = await database_sync_to_async(lambda: player.hand_cards)()
        self.player.is_blind = False
        self.player.hand_cards = cards
        logger.debug(f"[toggle_seen] Cached in-memory hand_cards={self.player.hand_cards}")

        # 3️⃣ Update the game state AFTER the player is revealed
        # This will handle pot, current_turn, round_status, etc.
        logger.debug(f"[toggle_seen] Game state updated to 'betting'. "
                    f"Pot={self.game_room.current_pot}, Round Status={self.game_room.round_status}")

        # 4️⃣ Send game data to all players
        await self.send_game_data()
        logger.debug(f"[toggle_seen] Game data sent after toggle_seen.")

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
        from .models import Player
        """Removes a player from the game room."""
        player.delete()
        

        # Check if room is now empty
        if not game_room.players.exists():
            game_room.delete()
            logger.debug(f"Deleted empty game room {game_room.id}")

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
                logger.debug(f"Declaring winner: {winner.user.username}")
                pot = self.game_room.current_pot
                logger.debug(f"Awarding pot of {pot} to {winner.user.username}")
                # Credit coins
                winner.user.coins += pot

                await self.add_earned_coins(winner.user, pot)

                await database_sync_to_async(winner.user.save)(update_fields=['coins'])
                # Reset pot
                self.game_room.current_pot = 0
                await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])

                # Save history
                await self.save_game_history(winner, 'win', pot)
                await self.update_challenge_progress(winner, hand_type=None)


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
        from datetime import datetime, timezone
        logger.debug(f"Entering next_turn with active_players_count: {active_players_count}")
        await self.cancel_timer()  # Cancel existing timer
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

            current_index = self.game_room.current_turn
            total_players = len(all_players)
            logger.debug(f"Current turn index: {current_index}, total players: {total_players}")

            next_index = (current_index + 1) % total_players
            searched = 0

            while searched < total_players:
                candidate = all_players[next_index]

                if candidate.is_packed or candidate.is_spectator:
                    logger.debug(f"Skipping player {candidate.user.username} (packed or spectator)") 
                    next_index = (next_index + 1) % total_players
                    searched += 1
                    continue
                
                #Set turn to candiate regardless of disconnect status
                self.game_room.current_turn = next_index
                await database_sync_to_async(self.game_room.save)(update_fields=["current_turn"])
                logger.debug(f"Next turn set to index {next_index} - player: {candidate.user.username}")

            
                if candidate.disconnected_at:
                    time_since_disconnect = (datetime.now(timezone.utc) - candidate.disconnected_at).total_seconds()
                    reconnect_key = f"reconnect_{self.game_room_id}_{candidate.user.id}"
                    task = RECONNECT_TIMERS.get(reconnect_key)

                    if time_since_disconnect > 10:
                        logger.debug(f"Player {candidate.user.username} disconnected for more than 10 seconds, marking as packed")
                        candidate.is_packed = True
                        await database_sync_to_async(candidate.save)(update_fields=['is_packed'])
                        await self.channel_layer.group_send(
                            self.room_group_name,
                            {
                                "type":"player_packed",
                                "player_id":candidate.user.id,
                                "message":f"Player {candidate.user.username} packed due to disconnection timeout. "

                            }
                        )
                        if task:
                            task.cancel()
                            del RECONNECT_TIMERS[reconnect_key]
                        next_index = (next_index + 1) % total_players
                        searched += 1
                        continue
                    elif task:  # Reconnect timer still active
                        logger.debug(f"Player {candidate.user.username} is disconnected, but reconnect timer is active")
                        # Allow timer to run in background, but set turn and start timer
                        self.game_room.current_turn = next_index
                        await database_sync_to_async(self.game_room.save)(update_fields=["current_turn"])
                        await self.start_timer(candidate)
                        await self.send_game_data()
                        return  # Let reconnect timer handle packing if it expires

                # Valid player found (not packed, not spectator, not disconnected)
                self.game_room.current_turn = next_index
                await database_sync_to_async(self.game_room.save)(update_fields=["current_turn"])
                logger.debug(f"Next turn set to index {next_index} - player: {candidate.user.username}")
                # Check if the current player is a bot
                if await database_sync_to_async(lambda: candidate.user.is_bot)():
                    logger.debug(f"Bot turn: {candidate.user.username}")
                    # Schedule bot action instead of starting timer
                    await self.start_timer(candidate)
                    asyncio.create_task(self.bot_perform_action(candidate))
                else:
                    # For real players, start timer as usual
                    await self.start_timer(candidate)
                await self.send_game_data()
                return

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

        pot = self.game_room.current_pot
        logger.debug(f"Awarding pot of {pot} to {winner.user.username}")
        # Credit coins
        winner.user.coins += pot
        await self.add_earned_coins(winner.user, pot)

        await database_sync_to_async(winner.user.save)(update_fields=['coins'])
        # Reset pot
        self.game_room.current_pot = 0
        await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])
        # Save history
        await self.save_game_history(winner, 'win', pot)
        await self.update_challenge_progress(winner, hand_type=winner.hand_type)


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
        from .models import GameTable
        """Delays the restart of the round by 10 seconds."""
        # 🛑 Make sure the game room still exists
        # Cancel timers for this game room
        timer_task = TURN_TIMERS.pop(self.game_room_id, None)
        if timer_task and not timer_task.done():
            timer_task.cancel()
            try:
                await timer_task
            except asyncio.CancelledError:
                logger.debug(f"Timer task for game room {self.game_room_id} canceled")

        # Also clear TIMER_TRACKER for all players in this room
        for pid in list(TIMER_TRACKER.keys()):
            TIMER_TRACKER.pop(pid, None)

        logger.debug(f"[delayed_restart_round] Starting for game room {self.game_room_id}")
        await asyncio.sleep(2)  # Short delay to ensure previous messages are sent
        game_room = await database_sync_to_async(
        lambda: GameTable.objects.filter(pk=self.game_room_id).first()
        )()
        if not game_room:
            logger.debug(f"[delayed_restart_round] Game room {self.game_room_id} deleted, skipping restart.")
            return

        logger.debug("[delayed_restart_round] Cleaning up disconnected or bankrupt players before 10s delay...")

        try:
            all_players = await database_sync_to_async(lambda: list(
                game_room.players.select_related("user").all()
            ))()
        except Exception as e:
            logger.error(f"[delayed_restart_round] Failed fetching players: {e}")
            return
        boot_amount = 100

        # Pre-cleanup: remove disconnected or bankrupt players
        for player in list(all_players):
            try:
                user_coins = await database_sync_to_async(lambda: player.user.coins)()
            except Exception:
                logger.warning(f"[delayed_restart_round] Player {getattr(player.user, 'id', 'unknown')} missing user row, removing.")
                await self.remove_player(player, game_room)
                if self.player and player.user.id == self.player.user.id:
                    logger.debug(f"[delayed_restart_round] Current consumer's player {player.user.username} removed, clearing self.player")
                    self.player = None
                continue

            if player.disconnected_at or user_coins < boot_amount:
                logger.debug(
                    f"[delayed_restart_round] Pre-restart cleanup: Removing player {player.user.username} (coins={user_coins}, disconnected={player.disconnected_at})"
                )
                # Cancel any reconnect timer for this player
                reconnect_key = f'reconnect_{self.game_room_id}_{player.user.id}'
                task = RECONNECT_TIMERS.get(reconnect_key)
                if task:
                    task.cancel()
                    RECONNECT_TIMERS.pop(reconnect_key, None)
                    logger.debug(f"[delayed_restart_round] Cancelled reconnect timer for player {player.user.username}")

                try:
                    await self.remove_player(player, game_room)
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            "type": "player_disconnected",
                            "player_id": player.user.id,
                            "message": f"Player {player.user.username} removed before restart."
                        }
                    )
                    if self.player and player.user.id == self.player.user.id:
                        logger.debug(f"[delayed_restart_round] Current consumer's player {player.user.username} removed, clearing self.player")
                        self.player = None
                        # 🔹 NEW: Close the consumer to prevent further actions
                        await self.close(code=4000)
                        return
                except Exception as e:
                    logger.error(f"[delayed_restart_round] Error removing player {player.user.username}: {e}")

        # 🔹 NEW: Remove all bot players to allow new real players to join
        logger.debug("[delayed_restart_round] Removing all bot players before 10-second delay...")
        bot_players = await database_sync_to_async(lambda: list(
            game_room.players.filter(user__is_bot=True).select_related('user')
        ))()
        for bot in bot_players:
            try:
                logger.debug(f"[delayed_restart_round] Removing bot: {bot.user.username} (ID: {bot.user.id})")
                await self.remove_player(bot, game_room)
                await database_sync_to_async(bot.user.delete)()  # Delete bot user as well
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "player_disconnected",
                        "player_id": bot.user.id,
                        "message": f"Bot {bot.user.username} removed to allow new players to join."
                    }
                )
            except Exception as e:
                logger.error(f"[delayed_restart_round] Error removing bot {bot.user.username}: {e}")

        # 🔹 Step 4: Re-fetch game room after cleanup
        game_room = await database_sync_to_async(
            lambda: GameTable.objects.filter(pk=self.game_room_id).first()
        )()
        if not game_room:
            logger.debug(f"[delayed_restart_round] Game room {self.game_room_id} deleted during cleanup.")
            return

        # 🔹 Step 5: Reset remaining real players
        all_players = await database_sync_to_async(lambda: list(
            game_room.players.select_related('user').all()
        ))()
        if not all_players:
            logger.debug(f"[delayed_restart_round] No players remain, deleting game room {self.game_room_id}.")
            await self.delete_game_room()
            return
        
        @database_sync_to_async
        def reset_players(players, boot_amount):
            with transaction.atomic():
                for player in players:
                    
                    player.is_blind = True
                    player.is_spectator = False
                    player.current_bet = 0
                    player.disconnected_at = None
                    player.save()
                    logger.debug(f" [delayed_restart_round] Reset player {player.user.username}")

        await reset_players(all_players, boot_amount=100)
        # After cleanup, let clients refresh before round starts
        exists = await database_sync_to_async(
        lambda: GameTable.objects.filter(pk=self.game_room_id).exists()
        )()
        if exists:
            await database_sync_to_async(
                lambda: GameTable.objects.filter(pk=self.game_room_id).update(is_active=False)
            )()
            logger.debug(f"[delayed_restart_round] Game room {self.game_room_id} marked inactive.")
        else:
            logger.debug(f"[delayed_restart_round] Game room {self.game_room_id} no longer exists, skipping is_active update.")
            return
        
        self.game_room = game_room
        if not self.player:
            logger.debug(f"[delayed_restart_round] Skipping send_game_data for game room {self.game_room_id} as self.player is None")            
        
        await self.send_game_data()
        logger.debug("Waiting 10 seconds before restarting round...")
        await asyncio.sleep(10)  # Wait 10 seconds to allow result display

        # Refresh or re-fetch the game room from DB to ensure it's still valid
        game_room = await database_sync_to_async(
            lambda: GameTable.objects.filter(pk=self.game_room_id).first()
        )()

        if not game_room:
            logger.debug(f"[delayed_restart_round] Game room {self.game_room_id} deleted during delay, skipping restart.")
            return
        
        self.game_room = game_room
        if game_room:
            asyncio.create_task(self.restart_round())
            logger.debug(f"[delayed_restart_round] Scheduled restart_round for game room {self.game_room_id}")
        else:
            logger.debug(f"[delayed_restart_round] Game room {self.game_room_id} no longer exists, skipping restart.")


    async def restart_round(self):
        from .models import GameTable
        """Restarts the game round with card distribution and betting."""
        logger.debug(f"[restart_round] Restarting round for game room {self.game_room_id}")

        # Refresh game room to ensure latest state
        if not self.game_room:
            logger.error(f"[restart_round] self.game_room is None, attempting to re-fetch for game_room_id {self.game_room_id}")
            self.game_room = await database_sync_to_async(lambda: GameTable.objects.filter(pk=self.game_room_id).first())()
            if not self.game_room:
                logger.error(f"[restart_round] Game room {self.game_room_id} does not exist, aborting restart")
                return

        # 🔹 Step 3: Refresh game room to ensure latest state
        game_room = await database_sync_to_async(lambda: GameTable.objects.filter(pk=self.game_room_id).first())()
        if not game_room:
            logger.debug(f"[restart_round] Game room {self.game_room_id} missing at restart, skipping.")
            return

        if game_room.is_active or game_room.round_status in ['betting', 'distribution']:
            logger.debug(f"[restart_round] Game room {self.game_room_id} already active or in progress "
                        f"(is_active={game_room.is_active}, round_status={game_room.round_status}), skipping restart")
            return

        # Fetch all players (already reset in delayed_restart_round)
        all_players = await database_sync_to_async(
            lambda: list(game_room.players.filter(disconnected_at__isnull=True))
        )()      
        player_count = len(all_players)
        # Adjust bots based on real player count
        if getattr(self.game_room, "is_private", False):
            logger.debug(f"Skipping bot addition: Table {self.game_room.id} is private.")
        else:
            await self.adjust_bots()

        # Re-fetch players after bot adjustment
        all_players = await database_sync_to_async(
            lambda: list(game_room.players.filter(disconnected_at__isnull=True))
        )()
        player_count = len(all_players)
        @database_sync_to_async
        def reset_players(players):
            with transaction.atomic():
                for player in players:
                    player.is_packed = False
                    player.hand_cards = []  # Clear hand cards
                    player.save()
        
        await reset_players(all_players)

        logger.debug(f"[restart_round] Player count for new round: {player_count}")
        if player_count == 0:
            logger.debug("[restart_round] No players remain, deleting game room.")
            await self.delete_game_room()
            await self.safe_send({
                'message': 'Game room closed due to no players!'
            })
            return
        
        if player_count < 2:
            logger.debug("[restart_round] Not enough players after boot, ending round")
            await self.update_game_state("waiting")
            await self.safe_send({
                'message': 'Not enough players to continue, waiting for more players!'
            })
            return
        # Additional logging to verify hand_cards are cleared after reset
        @database_sync_to_async
        def log_player_cards(players):
            for player in players:
                player.refresh_from_db()
                logger.debug(f"[restart_round] After reset - Player {player.user.username} - is_packed: {player.is_packed} - hand_cards: {player.hand_cards} - disconnected_at: {player.disconnected_at}")
        await log_player_cards(all_players)

        active_players = await self.get_active_players()
        logger.debug(f"[restart_round] Active players after reset: {[p.user.username for p in active_players]}, count: {len(active_players)}")

        self.game_room.current_pot = 0
        self.game_room.current_turn = 0
        await self.update_game_state("distribution")
        await database_sync_to_async(game_room.save)(update_fields=['current_pot', 'current_turn'])
        self.game_room = game_room

        await self.distribute_cards(player_count)

        # Additional logging to verify hand_cards after card distribution
        await log_player_cards(active_players)

        boot_amount = 100
        for player in active_players:
            try:
                logger.debug(f"[restart_round] Processing boot for {player.user.username}, coins before: {player.user.coins}")
                if player.user.coins < boot_amount:
                    logger.debug(f"[restart_round] Insufficient coins for {player.user.username}, packing player")
                    player.is_packed = True
                    await database_sync_to_async(player.save)()
                    continue

                if player.current_bet == boot_amount:
                    continue

                player.current_bet = boot_amount
                await self.update_player_coins(player, -boot_amount)
                await self.update_game_room_pot(boot_amount)
                await database_sync_to_async(player.save)()
                logger.debug(f"[restart_round] Boot applied for {player.user.username}, coins after: {player.user.coins}")
            except Exception as e:
                logger.error(f"[restart_round] Error applying boot for {player.user.username}: {e}")

        player_count = await self.get_active_players_count()
        if player_count < 2:
            logger.debug("[restart_round] Not enough players after boot, ending round")
            await self.update_game_state("waiting")
            await self.safe_send({
                'message': 'Not enough players to continue, waiting for more players!'
            })
            return
        # Begin betting round
        await self.update_game_state("betting")
        logger.debug(f"[restart_round] New round started with pot = {self.game_room.current_pot}, is_active = {self.game_room.is_active}")
        self.game_room.is_active = True
        await database_sync_to_async(game_room.save)(update_fields=['is_active'])
        self.game_room = game_room
        logger.debug(f"[restart_round] New round: is_active = {self.game_room.is_active}, round_status = {self.game_room.round_status}")
        # 🔹 Step 9: Validate self.player before sending game data
        self.player = await self.get_player(self.scope["user"], self.game_room)
        if not self.player:
            logger.info(f"[restart round] Player {self.scope['user'].username} already removed, skipping update")
        
        
        await self.send_game_data()

        await self.start_current_turn_timer()

    @database_sync_to_async
    def delete_game_room(self):
        """Deletes the game room."""
        self.game_room.delete()
        logger.debug(f"Game room {self.game_room_id} deleted due to no players remaining.")

    async def compare_hands(self, player1, player2):
        hand1_value = await self.calculate_hand_value(player1.hand_cards)
        hand2_value = await self.calculate_hand_value(player2.hand_cards)
        rank_map = {
            6: "trail",
            5: "pure_sequence",
            4: "sequence",
            3: "color",
            2: "pair",
            1: "high_card"
        }
        player1.hand_type = rank_map[hand1_value['rank']]
        player2.hand_type = rank_map[hand2_value['rank']]
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

    async def show_result(self, event):
        logger.debug(f"Sending show_result to client: {event['message']}")
        await self.send(text_data=json.dumps({
            "type": "show_result",
            "message": event["message"],
            "winner_id": event["winner_id"],
            "hand_winner": event["hand_winner"],
            "hand_loser": event["hand_loser"],
            "active_players_cards": event["active_players_cards"],
        }))

    async def declare_winner(self, winner):
        """Declares the winner, awards the pot, logs coins, and starts the next round."""
        logger.debug(f"Declaring winner: {winner.user.username}")

        # Get current pot
        pot = self.game_room.current_pot if self.game_room.current_pot else 0

        if pot > 0:
            logger.debug(f"Awarding pot of {pot} to {winner.user.username}")

            # Log EarnedCoin BEFORE resetting pot
            await self.add_earned_coins(winner.user, pot)

            # Credit coins
            winner.user.coins += pot
            await database_sync_to_async(winner.user.save)(update_fields=['coins'])

            # Reset pot
            self.game_room.current_pot = 0
            await database_sync_to_async(self.game_room.save)(update_fields=['current_pot'])

        # Save history
        await self.save_game_history(winner, 'win', pot)
        await self.update_challenge_progress(winner, hand_type=None)


        # Notify all players
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "show_result",
                "message": f'{winner.user.username} wins the pot!',
                "winner_id": winner.user.id,
                "hand_winner": winner.hand_cards,
                "hand_loser": [],  # For generic declaration
                "active_players_cards": [
                    {"player_id": p.user.id, "username": p.user.username, "cards": p.hand_cards}
                    for p in await self.get_active_players()
                    ]
            }
        )

        # Schedule restart
        asyncio.create_task(self.delayed_restart_round())

    async def safe_send(self, data: dict):
        try:
            await self.send(text_data=json.dumps(data))
        except RuntimeError as e:
            logger.warning(f"[safe_send] WebSocket send failed: {e}")
        except Exception as e:
            logger.error(f"[safe_send] Unexpected error during send: {e}")

    async def save_game_history(self, player, action, amount):
        from .models import GameHistory
        await database_sync_to_async(GameHistory.objects.create)(
            game=self.game_room,
            player=player,
            game_id_snapshot=self.game_room.id,
            game_name_snapshot=self.game_room.name,
            player_id_snapshot=player.id,
            player_username_snapshot=player.user.username,
            action=action,
            amount=amount
        )

    async def add_earned_coins(self,user, amount):
        from django.utils.timezone import now
        from .models import EarnedCoin
        await database_sync_to_async(EarnedCoin.objects.create)(user=user, amount=amount, earned_at=now())

    async def adjust_bots(self):
        real_players_count = await database_sync_to_async(
            lambda: self.game_room.players.filter(user__is_bot=False).count()
        )()
        bot_players_count = await database_sync_to_async(
            lambda: self.game_room.players.filter(user__is_bot=True).count()
        )()

        if real_players_count == 1:
            desired_bots = 2
        elif real_players_count == 2:
            desired_bots = 1
        else:
            desired_bots = 0  # For 3+ real players, no bots

        if bot_players_count < desired_bots:
            # Add missing bots
            to_add = desired_bots - bot_players_count
            await self.add_bots(to_add)
        elif bot_players_count > desired_bots:
            # Remove excess bots
            to_remove = bot_players_count - desired_bots
            await self.remove_bots(to_remove)

        await self.send_game_data()

    @database_sync_to_async
    def add_bots(self, count):
        from .models import Player
        from authentication.models import User

        # Get real players' coins to calculate bot coins
        real_players = list(self.game_room.players.filter(user__is_bot=False).select_related('user'))
        if not real_players:
            average_coins = 1000  # Default if no real players (edge case)
        else:
            real_coins = [p.user.coins for p in real_players]
            average_coins = sum(real_coins) // len(real_coins)
        
        for _ in range(count):
            # Create bot user with realistic name and coins (80-120% of average)
            bot_name = random.choice(BOT_NAMES) + '_' + ''.join(random.choices(string.digits, k=4))
            bot_coins = random.randint(int(average_coins * 0.8), int(average_coins * 1.2))
            bot_user = User.objects.create(
                username=bot_name,
                coins=bot_coins,  
                is_bot=True
            )
            # Create player for bot
            Player.objects.create(user=bot_user, table=self.game_room)
            logger.debug(f"Added bot: {bot_name} with {bot_coins} coins")

    @database_sync_to_async
    def remove_bots(self, count):
        from .models import Player

        bots = list(self.game_room.players.filter(user__is_bot=True).select_related('user'))
        random.shuffle(bots)  # Randomly select which bots to remove
        for bot in bots[:count]:
            username = bot.user.username
            bot.delete()
            bot.user.delete()  # Optional: delete bot user if not reusing
            logger.debug(f"Removed bot: {username}")

    async def bot_perform_action(self, bot_player):
        # Simulate thinking time
        think_time = random.uniform(5, 15)
        await asyncio.sleep(think_time)
        logger.debug(f"Bot {bot_player.user.username} thinking for {think_time:.2f}s")

        # Refresh game state to ensure it's still bot's turn
        await database_sync_to_async(self.game_room.refresh_from_db)()
        all_players = await database_sync_to_async(lambda: list(
            self.game_room.players.select_related('user').order_by('id').all()
        ))()
        if self.game_room.current_turn >= len(all_players) or all_players[self.game_room.current_turn] != bot_player:
            logger.debug(f"No longer {bot_player.user.username}'s turn, skipping action")
            return

        # Calculate hand strength
        hand_value = await self.calculate_hand_value(bot_player.hand_cards)
        strength = hand_value['rank']  # 1-6, higher better

        # Decide action based on strength (simple AI)
        actions = ['place_bet', 'place_double_bet', 'pack']
        if strength >= 4:  # Good hand: less likely to pack, more likely to bet high
            action_weights = [0.4, 0.5, 0.1]  # bet, double, pack
        elif strength <= 2:  # Bad hand: more likely to pack
            action_weights = [0.3, 0.2, 0.5]
        else:  # Medium: balanced
            action_weights = [0.4, 0.3, 0.3]

        # Check if sideshow or show possible
        active_count = await self.get_active_players_count()
        if active_count == 2 and not bot_player.is_blind:
            actions.append('show')
            action_weights.append(0.3 if strength >= 4 else 0.1)
            action_weights = [w / sum(action_weights) for w in action_weights]  # Normalize

        # Sideshow if conditions met
        prev_player = await self.get_previous_active_player()
        if prev_player and not bot_player.is_blind and not prev_player.is_blind:
            actions.append('sideshow')
            action_weights.append(0.2 if strength >= 3 else 0.05)
            action_weights = [w / sum(action_weights) for w in action_weights]

        action = random.choices(actions, weights=action_weights)[0]
        logger.debug(f"Bot {bot_player.user.username} chose action: {action}")

        # Execute action
        if action == 'pack':
            await self.handle_pack(bot_player.user.id)
        elif action == 'show':
            await self.handle_show(bot_player.user.id)
        elif action == 'sideshow':
            bet_amount = await self.calculate_min_bet(bot_player)  # Or match prev
            await self.handle_sideshow(bot_player.user.id, prev_player.user.id, bet_amount)
        else:  # bet or double_bet
            min_bet = await self.calculate_min_bet(bot_player)
            max_bet = min(bot_player.user.coins, min_bet * 4)  # Arbitrary max
            amount = random.randint(min_bet, max_bet)
            if action == 'place_bet':
                await self.handle_bet(bot_player.user.id, amount)
            else:
                # For double, adjust if needed
                double_amount = min_bet * 2
                if double_amount <= bot_player.user.coins:
                    await self.handle_double_bet(bot_player.user.id, double_amount)
                else:
                    await self.handle_bet(bot_player.user.id, amount)

    async def get_previous_active_player(self):
        await database_sync_to_async(self.game_room.refresh_from_db)()
        all_players = await database_sync_to_async(lambda: list(
            self.game_room.players.select_related('user').order_by('id').all()
        ))()
        current_index = self.game_room.current_turn
        prev_index = (current_index - 1) % len(all_players)
        searched = 0
        while searched < len(all_players):
            candidate = all_players[prev_index]
            if not candidate.is_packed and not candidate.is_spectator:
                return candidate
            prev_index = (prev_index - 1) % len(all_players)
            searched += 1
        return None

    # Helper to calculate min bet for bot (similar to handle_bet logic)
    async def calculate_min_bet(self, player):
        prev_player = await self.get_previous_active_player()
        if not prev_player:
            return 1
        min_bet = prev_player.current_bet
        if player.is_blind:
            min_bet = prev_player.current_bet if prev_player.is_blind else prev_player.current_bet // 2
        else:
            min_bet = prev_player.current_bet * 2 if prev_player.is_blind else prev_player.current_bet
        return min_bet
    


    @database_sync_to_async
    def update_challenge_progress(self,winner, hand_type):
        from .models import PlayerChallenge
        challenges = PlayerChallenge.objects.filter(player=winner.user, completed=False)

        for pc in challenges:
            if pc.challenge.type == "games":
                pc.progress += 1
            elif pc.challenge.type == "pair" and hand_type == "pair":
                pc.progress += 1
            elif pc.challenge.type == "high_card" and hand_type == "high_card":
                pc.progress += 1

            if pc.progress >= pc.challenge.goal:
                pc.completed = True
                winner.user.coins += pc.challenge.reward
                winner.user.save(update_fields=["coins"])
            pc.save()
