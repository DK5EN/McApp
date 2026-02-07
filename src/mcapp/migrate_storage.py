#!/usr/bin/env python3
"""
Migration script to convert JSON message dumps to SQLite database.

Usage:
    python migrate_storage.py --input ~/mcdump.json --output ~/mcapp.db
    python migrate_storage.py --input ~/mcdump.json --output ~/mcapp.db --dry-run
"""
import argparse
import asyncio
import sys
from pathlib import Path

from .logging_setup import get_logger, setup_logging
from .sqlite_storage import create_sqlite_storage

logger = get_logger(__name__)


async def migrate(
    input_file: str,
    output_db: str,
    dry_run: bool = False,
) -> int:
    """
    Migrate JSON dump to SQLite database.

    Args:
        input_file: Path to JSON dump file
        output_db: Path for SQLite database
        dry_run: If True, only validate without writing

    Returns:
        Number of messages migrated
    """
    input_path = Path(input_file)
    output_path = Path(output_db)

    if not input_path.exists():
        logger.error("Input file not found: %s", input_file)
        return 0

    if output_path.exists() and not dry_run:
        logger.warning("Output database exists: %s", output_db)
        response = input("Overwrite? [y/N]: ")
        if response.lower() != "y":
            logger.info("Migration cancelled")
            return 0
        output_path.unlink()

    logger.info("Starting migration from %s to %s", input_file, output_db)

    if dry_run:
        logger.info("DRY RUN - no changes will be made")
        # Just count messages in input file
        import json

        with open(input_path, encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Would migrate %d messages", len(data))
        return len(data)

    # Create SQLite storage and load data
    storage = await create_sqlite_storage(output_db)
    count = await storage.load_dump(input_file)

    # Verify
    total = await storage.get_message_count()
    size_mb = await storage.get_storage_size_mb()

    logger.info("Migration complete!")
    logger.info("  Messages loaded: %d", count)
    logger.info("  Total in database: %d", total)
    logger.info("  Database size: %.2f MB", size_mb)

    return count


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Migrate McApp JSON dumps to SQLite database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python migrate_storage.py --input ~/mcdump.json --output ~/mcapp.db
    python migrate_storage.py --input ~/mcdump.json --output ~/mcapp.db --dry-run
        """,
    )
    parser.add_argument(
        "--input",
        "-i",
        required=True,
        help="Path to input JSON dump file",
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="Path for output SQLite database",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Validate without writing to database",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(verbose=args.verbose, simple_format=True)

    # Run migration
    count = asyncio.run(migrate(args.input, args.output, args.dry_run))

    if count > 0:
        logger.info("Successfully processed %d messages", count)
        sys.exit(0)
    else:
        logger.error("Migration failed or no messages found")
        sys.exit(1)


if __name__ == "__main__":
    main()
