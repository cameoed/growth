# Use your own Oracle VM

The VM runs the private HTTP service and stores the SQLite database. The
dashboard reaches it over SSH. You can run rankings on the VM or have your
desktop compute them while the database stays on the VM. Connection profiles
live in the checkout's ignored `data/` directory. OpenSSH reads the private key
from the file you select.

## Prepare the VM

1. Set up an Ubuntu 24.04 or newer instance in your Oracle Cloud account.
   Enable key-based SSH for the Ubuntu user. Restrict SSH ingress to the
   addresses you use. Keep TCP port 8787 private.
2. Get the server's SSH host key through a trusted console session. Use
   `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` there to check the
   fingerprint and read the public key with
   `cat /etc/ssh/ssh_host_ed25519_key.pub`. Add that public key to a local
   known-hosts file, using the exact VM hostname or IPv4 address you will
   enter in the app. For a nondefault port, use `[VM_HOST]:PORT` as the
   known-hosts name. The known-hosts line contains the hostname, key type
   and public key. Keep the private user key readable only by your account.
   For a passphrase-protected key, load it into the SSH agent available to the app.
   [Oracle SSH documentation](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/connect-to-linux-instance.htm)
3. From your source checkout, create a private setup profile under `data/`.
   Replace the example host and paths with your own. Restrict access to your
   account. On macOS and Linux, set the file mode to 0600.

   ```json
   {
     "enabled": true,
     "host": "vm.example.com",
     "user": "ubuntu",
     "ssh_port": 22,
     "identity_file": "/absolute/path/to/your/ssh-private-key",
     "known_hosts_file": "/absolute/path/to/your/known-hosts",
     "local_port": 18787
   }
   ```

   On Windows, use absolute paths such as `C:/path/to/id_ed25519`
   and `C:/path/to/known_hosts` in the profile. Run `Get-Command ssh`
   in PowerShell to check that OpenSSH Client is installed.
   [Microsoft OpenSSH installation guide](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse)

4. Run the two commands below with the setup profile. The Ubuntu SSH user needs
   sudo for installation. This builds the required SQLite runtime, installs
   the app and starts a loopback-only service with the starter companies on a new VM.

   ```sh
   chmod 600 data/oracle-setup.json
   .venv/bin/python scripts/oracle_cloud.py install --config data/oracle-setup.json
   .venv/bin/python scripts/oracle_cloud.py start-empty --config data/oracle-setup.json
   ```

   On Windows PowerShell:

   ```powershell
   .\.venv\Scripts\python.exe scripts/oracle_cloud.py install --config data/oracle-setup.json
   .\.venv\Scripts\python.exe scripts/oracle_cloud.py start-empty --config data/oracle-setup.json
   ```

## Connect on macOS

Open **Beings Growth → Storage Mode** or **Settings → Storage Mode →
Change storage mode**. **Local Mode** is selected on fresh installs. Choose
**Virtual Machine** to reveal the VM host, SSH user, SSH port, private-key
path, trusted known-hosts path, and local tunnel port. The default tunnel
port is 18787. **Verify & Save** checks the pinned host, SSH authentication,
and the private Beings Growth service. Quit and reopen to apply the choice.

The Mac app serves its bundled dashboard on the configured loopback port and sends
API requests through SSH. Scans and SQLite run on the VM. If the app reports that
the VM needs an update, update the VM backend before reconnecting.

To switch from an active VM to this Mac, select **Local Mode** while connected.
The sheet shows the database size and free space on the Mac. Choose **Proceed**
to download and verify a SQLite snapshot, then reopen the app to use it. If a
local database already exists, the sheet blocks the download to protect that
store. The app remembers your VM profile. Provider credentials are stored
separately on the backend where you configured them.

## Connect on Windows or in a browser

Keep the tunnel command running in a terminal and open
[the tunneled dashboard](http://127.0.0.1:18787/) or the `local_port` in your
setup profile. The local dashboard at port 8787 is a separate store.

**Windows PowerShell**

```powershell
.\.venv\Scripts\python.exe scripts/oracle_cloud.py tunnel --config data/oracle-setup.json
```

**macOS and Linux**

```sh
.venv/bin/python scripts/oracle_cloud.py tunnel --config data/oracle-setup.json
```

If the app cannot connect, check the VM's SSH ingress and host-key pin, ensure
SSH permits local TCP forwarding, and check that `beings-growth.service` is
running on the VM. The dashboard port stays bound to loopback.

## Compute rankings on your desktop

Use matching source versions on your desktop and VM. On the VM, run
`sudo systemctl edit beings-growth.service` and add this service override:

```ini
[Service]
Environment=BEINGS_DESKTOP_RANKING_SOCKET=/run/beings-growth/ranking.sock
```

Restart `beings-growth.service`. The native Mac app starts its ranking worker
automatically. For a browser connection, append `--desktop-ranking` to the
tunnel command above. Keep the terminal open while rankings run. The VM keeps
the database and saves the results. One desktop worker computes the shared
queue at a time. Rankings wait for that worker while this setting is enabled.
The default VM setup computes rankings on the server.
