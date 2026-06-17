"""Tests for context search improvements — wider default window + per-source retention."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from assistant_agent.config import AppConfig
from assistant_agent.context_store import ContextStore
from assistant_agent.database import Database


class ContextSearchTest(unittest.TestCase):
    """Test context search window configuration and recent_only flag."""

    def setUp(self):
        """Set up test fixtures."""
        self.db = MagicMock(spec=Database)
        self.config = AppConfig({
            "agent": {
                "context": {
                    "search_days": 90
                }
            }
        })
        self.store = ContextStore(self.db, self.config)

    def test_default_search_days_is_90(self):
        """Test that default search_days is 90 from config."""
        self.assertEqual(self.store.search_days(), 90)

    def test_search_days_override_in_search_method(self):
        """Test that search() accepts search_days override parameter."""
        # Mock embedding client to return None (force keyword search path)
        with patch.object(self.store, '_embed_query', return_value=None):
            with patch.object(self.store, '_keyword_search', return_value=[]) as mock_keyword:
                self.store.search("test query", limit=10, search_days=7)
                # Verify _keyword_search was called with search_days=7
                mock_keyword.assert_called_once()
                args, kwargs = mock_keyword.call_args
                self.assertEqual(kwargs.get('search_days'), 7)

    def test_search_without_override_uses_default(self):
        """Test that search() without search_days uses configured default."""
        with patch.object(self.store, '_embed_query', return_value=None):
            with patch.object(self.store, '_keyword_search', return_value=[]) as mock_keyword:
                self.store.search("test query", limit=10)
                # Verify _keyword_search was called without search_days override
                mock_keyword.assert_called_once()
                args, kwargs = mock_keyword.call_args
                self.assertIsNone(kwargs.get('search_days'))

    def test_semantic_search_respects_search_days_override(self):
        """Test that _semantic_search uses search_days override when provided."""
        mock_embedding = [0.1] * 768
        
        # Mock the DB calls for each source
        self.db.fetch_all.return_value = []
        
        with patch.object(self.store, '_search_memories_semantic', return_value=[]):
            with patch.object(self.store, '_search_jobs_semantic', return_value=[]) as mock_jobs:
                with patch.object(self.store, '_search_reminders_semantic', return_value=[]):
                    with patch.object(self.store, '_search_outbound_emails_semantic', return_value=[]):
                        with patch.object(self.store, '_search_inbound_emails_semantic', return_value=[]):
                            with patch.object(self.store, '_search_notes_semantic', return_value=[]):
                                with patch.object(self.store, '_search_projects_semantic', return_value=[]):
                                    with patch.object(self.store, '_search_contacts_semantic', return_value=[]):
                                        self.store._semantic_search("test", mock_embedding, 10, search_days=7)
                                        
                                        # Verify jobs search was called with days=7
                                        mock_jobs.assert_called_once()
                                        args = mock_jobs.call_args[0]
                                        self.assertEqual(args[3], 7)  # days parameter

    def test_semantic_search_without_override_uses_default(self):
        """Test that _semantic_search uses configured default when no override."""
        mock_embedding = [0.1] * 768
        
        self.db.fetch_all.return_value = []
        
        with patch.object(self.store, '_search_memories_semantic', return_value=[]):
            with patch.object(self.store, '_search_jobs_semantic', return_value=[]) as mock_jobs:
                with patch.object(self.store, '_search_reminders_semantic', return_value=[]):
                    with patch.object(self.store, '_search_outbound_emails_semantic', return_value=[]):
                        with patch.object(self.store, '_search_inbound_emails_semantic', return_value=[]):
                            with patch.object(self.store, '_search_notes_semantic', return_value=[]):
                                with patch.object(self.store, '_search_projects_semantic', return_value=[]):
                                    with patch.object(self.store, '_search_contacts_semantic', return_value=[]):
                                        self.store._semantic_search("test", mock_embedding, 10)
                                        
                                        # Verify jobs search was called with days=90 (default)
                                        mock_jobs.assert_called_once()
                                        args = mock_jobs.call_args[0]
                                        self.assertEqual(args[3], 90)

    def test_keyword_search_respects_search_days_override(self):
        """Test that _keyword_search uses search_days override when provided."""
        self.db.fetch_all.return_value = []
        
        with patch.object(self.store, '_search_memories_keyword', return_value=[]):
            with patch.object(self.store, '_search_jobs_keyword', return_value=[]) as mock_jobs:
                with patch.object(self.store, '_search_reminders_keyword', return_value=[]):
                    with patch.object(self.store, '_search_outbound_emails_keyword', return_value=[]):
                        with patch.object(self.store, '_search_inbound_emails_keyword', return_value=[]):
                            with patch.object(self.store, '_search_notes_keyword', return_value=[]):
                                with patch.object(self.store, '_search_projects_keyword', return_value=[]):
                                    with patch.object(self.store, '_search_contacts_keyword', return_value=[]):
                                        self.store._keyword_search("test", 10, search_days=7)
                                        
                                        # Verify jobs search was called with days=7
                                        mock_jobs.assert_called_once()
                                        args = mock_jobs.call_args[0]
                                        self.assertEqual(args[2], 7)  # days parameter

    def test_fallback_to_default_when_search_days_30(self):
        """Test fallback to 30 days when config is not present."""
        config_no_search_days = AppConfig({"agent": {}})
        store_no_config = ContextStore(self.db, config_no_search_days)
        self.assertEqual(store_no_config.search_days(), 30)


class ContextSearchToolTest(unittest.TestCase):
    """Test context_search tool method with recent_only flag."""

    def setUp(self):
        """Set up test fixtures."""
        self.db = MagicMock(spec=Database)
        self.config = AppConfig({
            "agent": {
                "context": {
                    "search_days": 90
                },
                "filesystem": {
                    "shared_root": "/data/share"
                }
            }
        })

    def test_context_search_with_recent_only_true(self):
        """Test that context_search with recent_only=True passes search_days=7."""
        from assistant_agent.tools import ToolRuntime
        
        job = {"id": 1, "thread_id": 1}
        runtime = ToolRuntime(self.db, self.config, job)
        
        with patch.object(ContextStore, 'search', return_value=[]) as mock_search:
            result = runtime.context_search("test query", limit=10, recent_only=True)
            
            # Verify search was called with search_days=7
            mock_search.assert_called_once()
            args, kwargs = mock_search.call_args
            self.assertEqual(kwargs.get('search_days'), 7)
            self.assertEqual(result['query'], "test query")
            self.assertEqual(result['result_count'], 0)

    def test_context_search_with_recent_only_false(self):
        """Test that context_search with recent_only=False uses full window."""
        from assistant_agent.tools import ToolRuntime
        
        job = {"id": 1, "thread_id": 1}
        runtime = ToolRuntime(self.db, self.config, job)
        
        with patch.object(ContextStore, 'search', return_value=[]) as mock_search:
            result = runtime.context_search("test query", limit=10, recent_only=False)
            
            # Verify search was called without search_days override (uses default 90)
            mock_search.assert_called_once()
            args, kwargs = mock_search.call_args
            self.assertNotIn('search_days', kwargs)

    def test_context_search_default_recent_only(self):
        """Test that context_search defaults recent_only to False."""
        from assistant_agent.tools import ToolRuntime
        
        job = {"id": 1, "thread_id": 1}
        runtime = ToolRuntime(self.db, self.config, job)
        
        with patch.object(ContextStore, 'search', return_value=[]) as mock_search:
            result = runtime.context_search("test query", limit=10)
            
            # Verify search was called without search_days override
            mock_search.assert_called_once()
            args, kwargs = mock_search.call_args
            self.assertNotIn('search_days', kwargs)


if __name__ == "__main__":
    unittest.main()
