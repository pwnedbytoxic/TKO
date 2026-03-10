import socket
import threading
import re

HOST = "0.0.0.0"
PORT = 9339

def send(conn, xml):
    conn.send((xml + "\0").encode())

def handle_client(conn, addr):
    print("Socket client connected:", addr)

    try:
        while True:
            data = conn.recv(4096)
            if not data:
                break

            msg = data.decode(errors="ignore").strip("\0")
            print("Received:", msg)

            if "verChk" in msg:
                send(conn,"<msg t='sys'><body action='apiOK' r='0'/></msg>")

            elif "login" in msg:

                send(conn,
                "<msg t='sys'><body action='logOK' r='0'><login id='1' mod='0' n='player'/></body></msg>")

                send(conn,
                "<msg t='sys'><body action='rmList' r='0'><rmList>"
                "<rm id='1' n='Lobby' maxu='10' temp='0' game='0' priv='0' limbo='0'/>"
                "</rmList></body></msg>")

                send(conn,
                "<msg t='sys'><body action='joinOK' r='0'><room id='1' n='Lobby'>"
                "<u id='1' n='player' mod='0'/>"
                "</room></body></msg>")

    except Exception as e:
        print("Socket error:", e)

    conn.close()
    print("Client disconnected")

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind((HOST, PORT))
s.listen()

print("SmartFox socket emulator listening on port", PORT)

while True:
    conn, addr = s.accept()
    threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()