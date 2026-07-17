/** PM2 config for OB scalp pick + follow.
 *
 *   pm2 start ecosystem.scalp.config.js
 *   pm2 save
 *
 * Uses .venv/bin/python (run: python3 -m venv .venv && pip install -r requirements-ml.txt)
 */
const path = require("path");

const root = __dirname;
const python = path.join(root, ".venv", "bin", "python");

module.exports = {
  apps: [
    {
      // Keep Mac awake even if display sleeps (desktop iMac trading)
      name: "keep-awake",
      script: "/usr/bin/caffeinate",
      args: "-dims",
      interpreter: "none",
      autorestart: true,
      max_restarts: 50,
      restart_delay: 2000,
    },
    {
      name: "scalp-pick",
      cwd: root,
      script: "ob_scalp_pick.py",
      interpreter: python,
      args: "--daemon -y --idle-min 90 --count 3",
      autorestart: true,
      max_restarts: 20,
      restart_delay: 5000,
    },
    {
      name: "scalp-follow",
      cwd: root,
      script: "ob_scalp_follow.py",
      interpreter: python,
      args: "--daemon",
      autorestart: true,
      max_restarts: 20,
      restart_delay: 3000,
    },
  ],
};
