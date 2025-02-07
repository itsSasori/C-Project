console.log("Loading Javascript");
console.log('Game Room ID:', gameRoomId);  // Check if the value is correct in the console

const socket = new WebSocket(`ws://${window.location.host}/ws/game/${gameRoomId}/`);

socket.onopen = function(e) {
    console.log("Connected to game room");
};

socket.onmessage = function(e) {
    const data = JSON.parse(e.data);
    console.log("Received game data:", data);
    updateGameUI(data);
    if (data.players) {
        updatePlayersList(data.players);
    }
    if (data.player_left) {
        // Handle player leaving scenario
        removePlayerFromUI(data.player_id);
    }
};

function removePlayerFromUI(playerId) {
    const playerElement = document.getElementById(`player-${playerId}`);
    if (playerElement) {
        playerElement.remove();
        console.log(`Player ${playerId} removed from UI`);
    }
}

function updatePlayersList(players) {
    const playerList = document.getElementById("player-list");
    playerList.innerHTML = "";

    players.forEach(player => {
        const li = document.createElement("li");
        li.textContent = `${player.username} (${player.status})`;
        playerList.appendChild(li);
    });
}

socket.onclose = function(e) {
    console.log("Disconnected from game room");
};

// function placeBet(amount) {
//     const data = {
//         action: 'place_bet',
//         player_id: 1,  // Player ID
//         amount: amount
//     };
//     socket.send(JSON.stringify(data));
// }

// function packGame() {
//     const data = {
//         action: 'pack',
//         player_id: {{ user.id }}
//         // Player ID
//     };
//     socket.send(JSON.stringify(data));
//     document.getElementById("pack-btn").disabled = true;
// }

// function requestSideshow() {
//     const data = {
//         action: 'sideshow',
//         player_id: {{ user.id }}  // Player ID
//     };
//     socket.send(JSON.stringify(data));
// }
function updateGameUI(data) {
    document.getElementById("table-pot").textContent = data.pot;
    document.getElementById("current-bet").textContent = `♦ ${data.current_bet}`;
    
    if (data.players) {
        data.players.forEach(player => {
            const playerElement = document.querySelector(`#player-${player.id} .player-score`);
            if (playerElement) {
                playerElement.textContent = player.status;
            }
        });
    }
}

// document.getElementById("pack-btn").addEventListener("click", packGame);
// document.getElementById("sideshow-btn").addEventListener("click", requestSideshow); 

// Check if the page is being loaded from history (back button)
window.addEventListener("popstate", function (event) {
    let userChoice = confirm("Do you want to continue playing? Click 'OK' to stay or 'Cancel' to return to the main page.");
    
    if (!userChoice) {
        // Redirect to the interface page and force reload
        window.location.href = "/interface-page-url"; // Replace with actual interface URL
        setTimeout(() => {
            window.location.reload();
        }, 100);
    }
});

// Ensures the interface page reloads when user navigates back
window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
        window.location.reload();
    }
});