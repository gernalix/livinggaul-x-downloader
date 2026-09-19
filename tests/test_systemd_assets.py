import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class SystemdAssetsTests(unittest.TestCase):
    def test_service_has_resilience_guards(self):
        text = (ROOT / "systemd" / "livinggaul-x-source-availability.service").read_text()
        for required in (
            "Type=oneshot",
            "Restart=on-failure",
            "RestartSec=5min",
            "TimeoutStartSec=50min",
            "/usr/bin/flock --exclusive --wait 30",
            "--check-source-availability --cookies-mode auto",
            "StandardOutput=journal",
            "NoNewPrivileges=true",
        ):
            self.assertIn(required, text)

    def test_timer_is_hourly_and_persistent(self):
        text = (ROOT / "systemd" / "livinggaul-x-source-availability.timer").read_text()
        self.assertIn("OnCalendar=hourly", text)
        self.assertIn("Persistent=true", text)
        self.assertIn("WantedBy=timers.target", text)

    def test_installer_enables_timer_and_linger(self):
        text = (ROOT / "systemd" / "install-user-service.sh").read_text()
        self.assertIn('systemctl --user enable --now "$timer_name"', text)
        self.assertIn('loginctl enable-linger "$USER"', text)
        self.assertIn('systemctl --user start "$service_name"', text)


if __name__ == "__main__":
    unittest.main()
