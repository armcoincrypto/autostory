"""
Professional tdata parser and validator for STORYFLEET
Analyzes Telegram Desktop session folders
"""
import os
import re
import sqlite3
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
import struct
import logging

logger = logging.getLogger(__name__)


@dataclass
class TdataAccount:
    """Validated tdata account"""
    path: Path
    folder_name: str
    phone_number: Optional[str] = None
    user_id: Optional[int] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    dc_id: Optional[int] = None
    has_key_data: bool = False
    has_map: bool = False
    has_session_files: bool = False
    is_valid: bool = False
    validation_error: Optional[str] = None
    file_count: int = 0
    total_size: int = 0
    last_modified: Optional[datetime] = None


class TdataAnalyzer:
    """
    Professional tdata analysis system

    Supports multiple tdata structures:
    - Single account: folder/tdata/...
    - Multi-account: folder/account1/tdata/..., folder/account2/tdata/...
    - Flat structure: tdata/...
    """

    ESSENTIAL_FILES = ['key_datas', 'key_data']
    SESSION_PATTERNS = [
        r'^[A-F0-9]{16}$',  # D877F783D5D3EF8C
        r'^[A-F0-9]{16}s$',  # D877F783D5D3EF8Cs
        r'^map\d*$',
        r'^configs$',
    ]

    def __init__(self, root_path: str):
        self.root_path = Path(root_path).resolve()
        self.accounts: List[TdataAccount] = []
        self.report = {
            'scan_time': datetime.now().isoformat(),
            'root_path': str(self.root_path),
            'total_folders_scanned': 0,
            'valid_accounts': 0,
            'invalid_accounts': 0,
            'errors': [],
            'warnings': []
        }

    def discover_accounts(self) -> List[TdataAccount]:
        """Find and analyze all tdata accounts"""
        if not self.root_path.exists():
            self.report['errors'].append(f"Path does not exist: {self.root_path}")
            return []

        logger.info(f"Scanning for tdata accounts in: {self.root_path}")

        discovered = []

        # Pattern 1: Root is tdata folder itself
        if self._is_tdata_folder(self.root_path):
            account = self._analyze_tdata_folder(self.root_path, self.root_path.name)
            discovered.append(account)

        # Pattern 2: Root contains tdata folder
        tdata_direct = self.root_path / 'tdata'
        if tdata_direct.exists() and self._is_tdata_folder(tdata_direct):
            account = self._analyze_tdata_folder(tdata_direct, self.root_path.name)
            discovered.append(account)

        # Pattern 3: Root contains multiple account folders
        for item in self.root_path.iterdir():
            if item.is_dir() and item.name not in ['tdata', '.', '..']:
                # Check for tdata subfolder
                tdata_path = item / 'tdata'
                if tdata_path.exists() and self._is_tdata_folder(tdata_path):
                    account = self._analyze_tdata_folder(tdata_path, item.name)
                    discovered.append(account)
                # Check if folder itself is tdata
                elif self._is_tdata_folder(item):
                    account = self._analyze_tdata_folder(item, item.name)
                    discovered.append(account)

        self.accounts = discovered
        self.report['total_folders_scanned'] = len(discovered)
        self.report['valid_accounts'] = sum(1 for a in discovered if a.is_valid)
        self.report['invalid_accounts'] = sum(1 for a in discovered if not a.is_valid)

        return discovered

    def _is_tdata_folder(self, path: Path) -> bool:
        """Check if folder looks like a tdata folder"""
        if not path.is_dir():
            return False

        # Check for key_data or key_datas
        has_key = any((path / f).exists() for f in self.ESSENTIAL_FILES)

        # Check for session pattern files
        has_session = any(
            re.match(pattern, f.name)
            for f in path.iterdir() if f.is_file()
            for pattern in self.SESSION_PATTERNS
        )

        return has_key or has_session

    def _analyze_tdata_folder(self, tdata_path: Path, folder_name: str) -> TdataAccount:
        """Analyze a single tdata folder"""
        account = TdataAccount(
            path=tdata_path,
            folder_name=folder_name
        )

        try:
            # Check essential files
            account.has_key_data = any(
                (tdata_path / f).exists() for f in self.ESSENTIAL_FILES
            )
            account.has_map = (tdata_path / 'map').exists() or (tdata_path / 'map0').exists()

            # Check for session files
            session_files = []
            for f in tdata_path.iterdir():
                if f.is_file():
                    for pattern in self.SESSION_PATTERNS:
                        if re.match(pattern, f.name):
                            session_files.append(f)
                            break
            account.has_session_files = len(session_files) > 0

            # Get file statistics
            account.file_count = sum(1 for _ in tdata_path.rglob('*') if _.is_file())
            account.total_size = sum(f.stat().st_size for f in tdata_path.rglob('*') if f.is_file())

            # Get last modified time
            all_files = list(tdata_path.rglob('*'))
            if all_files:
                latest = max(f.stat().st_mtime for f in all_files if f.is_file())
                account.last_modified = datetime.fromtimestamp(latest)

            # Try to extract account info
            account.phone_number = self._extract_phone(tdata_path)
            account.user_id = self._extract_user_id(tdata_path)
            account.dc_id = self._extract_dc_id(tdata_path)

            # Validate
            if not account.has_key_data:
                account.validation_error = "Missing key_data/key_datas file"
                account.is_valid = False
            elif not account.has_session_files:
                account.validation_error = "No session files found"
                account.is_valid = False
            else:
                account.is_valid = True

        except Exception as e:
            account.validation_error = f"Analysis error: {str(e)}"
            account.is_valid = False
            self.report['errors'].append(f"{folder_name}: {str(e)}")

        return account

    def _extract_phone(self, tdata_path: Path) -> Optional[str]:
        """Try to extract phone number from tdata"""
        # Method 1: Check sqlite databases
        for db_file in tdata_path.glob('*.db'):
            phone = self._extract_phone_from_db(db_file)
            if phone:
                return phone

        # Method 2: Check folder name for phone pattern
        folder_name = tdata_path.parent.name
        phone_match = re.search(r'\+?(\d{10,15})', folder_name)
        if phone_match:
            return '+' + phone_match.group(1).lstrip('+')

        # Method 3: Parse binary files (simplified)
        for f in ['configs', 'user_data']:
            file_path = tdata_path / f
            if file_path.exists():
                try:
                    with open(file_path, 'rb') as fp:
                        data = fp.read(1024)
                        # Look for phone number pattern in binary
                        matches = re.findall(rb'\+?\d{10,15}', data)
                        if matches:
                            return matches[0].decode('ascii', errors='ignore')
                except:
                    pass

        return None

    def _extract_phone_from_db(self, db_path: Path) -> Optional[str]:
        """Extract phone from SQLite database"""
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            # Try common table/column combinations
            queries = [
                "SELECT phone FROM users LIMIT 1",
                "SELECT value FROM settings WHERE key='phone'",
                "SELECT phone FROM accounts LIMIT 1",
            ]

            for query in queries:
                try:
                    cursor.execute(query)
                    result = cursor.fetchone()
                    if result and result[0]:
                        conn.close()
                        return str(result[0])
                except sqlite3.Error:
                    continue

            conn.close()
        except:
            pass

        return None

    def _extract_user_id(self, tdata_path: Path) -> Optional[int]:
        """Try to extract Telegram user ID"""
        # Check configs file
        configs_path = tdata_path / 'configs'
        if configs_path.exists():
            try:
                with open(configs_path, 'rb') as f:
                    data = f.read()
                    # User ID is often stored as 4-byte integer
                    # This is a simplified extraction
                    if len(data) >= 8:
                        potential_id = struct.unpack('<I', data[4:8])[0]
                        if 100000 < potential_id < 10000000000:  # Valid Telegram ID range
                            return potential_id
            except:
                pass

        return None

    def _extract_dc_id(self, tdata_path: Path) -> Optional[int]:
        """Extract datacenter ID"""
        key_path = tdata_path / 'key_datas'
        if not key_path.exists():
            key_path = tdata_path / 'key_data'

        if key_path.exists():
            try:
                with open(key_path, 'rb') as f:
                    data = f.read(4)
                    if len(data) >= 4:
                        dc_id = struct.unpack('<I', data)[0]
                        if 1 <= dc_id <= 5:  # Valid DC range
                            return dc_id
            except:
                pass

        return None

    def generate_report(self) -> Dict[str, Any]:
        """Generate comprehensive analysis report"""
        report = self.report.copy()

        report['accounts'] = []
        for acc in self.accounts:
            acc_info = {
                'folder': acc.folder_name,
                'path': str(acc.path),
                'phone': acc.phone_number,
                'user_id': acc.user_id,
                'dc_id': acc.dc_id,
                'valid': acc.is_valid,
                'error': acc.validation_error,
                'files': acc.file_count,
                'size_kb': round(acc.total_size / 1024, 2),
                'last_modified': acc.last_modified.isoformat() if acc.last_modified else None,
                'has_key_data': acc.has_key_data,
                'has_map': acc.has_map,
                'has_sessions': acc.has_session_files,
            }
            report['accounts'].append(acc_info)

        # Recommendations
        report['recommendations'] = []
        if report['valid_accounts'] == 0:
            report['recommendations'].append("No valid tdata accounts found. Check folder structure.")
        elif report['valid_accounts'] < 5:
            report['recommendations'].append(f"Only {report['valid_accounts']} valid accounts. Consider adding more for load distribution.")
        else:
            report['recommendations'].append(f"{report['valid_accounts']} accounts ready for import.")

        return report

    def format_report_text(self) -> str:
        """Format report for display"""
        report = self.generate_report()

        lines = [
            "📊 **TDATA ANALYSIS REPORT**",
            "",
            f"📁 Root: `{report['root_path']}`",
            f"🔍 Scanned: {report['total_folders_scanned']} folders",
            f"✅ Valid: {report['valid_accounts']}",
            f"❌ Invalid: {report['invalid_accounts']}",
            "",
            "**Accounts Found:**"
        ]

        for acc in report['accounts']:
            status = "✅" if acc['valid'] else "❌"
            phone = acc['phone'] or 'Unknown'
            lines.append(f"{status} {acc['folder']}: {phone}")
            if not acc['valid'] and acc['error']:
                lines.append(f"   └ Error: {acc['error']}")

        if report['recommendations']:
            lines.append("")
            lines.append("**Recommendations:**")
            for rec in report['recommendations']:
                lines.append(f"• {rec}")

        return "\n".join(lines)


# Convenience function
def analyze_tdata_folder(path: str) -> Dict[str, Any]:
    """Quick analysis of tdata folder"""
    analyzer = TdataAnalyzer(path)
    analyzer.discover_accounts()
    return analyzer.generate_report()
