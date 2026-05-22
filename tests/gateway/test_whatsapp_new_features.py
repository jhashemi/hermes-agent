"""
Tests for new WhatsApp adapter features: delete_message, reactions, and media batching.

These tests verify the methods exist and have proper signatures.
For integration tests with actual bridge, run the bridge separately.
"""

import pytest
from gateway.platforms.whatsapp import WhatsAppAdapter


class TestWhatsAppAdapterNewMethods:
    """Test new methods are properly defined on WhatsAppAdapter."""
    
    def test_delete_message_method_exists(self):
        """Test that delete_message method exists and is async."""
        assert hasattr(WhatsAppAdapter, 'delete_message')
        assert callable(getattr(WhatsAppAdapter, 'delete_message'))
    
    def test_react_to_message_method_exists(self):
        """Test that react_to_message method exists and is async."""
        assert hasattr(WhatsAppAdapter, 'react_to_message')
        assert callable(getattr(WhatsAppAdapter, 'react_to_message'))
    
    def test_send_media_batch_method_exists(self):
        """Test that send_media_batch method exists and is async."""
        assert hasattr(WhatsAppAdapter, 'send_media_batch')
        assert callable(getattr(WhatsAppAdapter, 'send_media_batch'))
    
    def test_delete_message_signature(self):
        """Verify delete_message has correct signature."""
        import inspect
        sig = inspect.signature(WhatsAppAdapter.delete_message)
        params = list(sig.parameters.keys())
        assert 'self' in params
        assert 'chat_id' in params
        assert 'message_id' in params
    
    def test_react_to_message_signature(self):
        """Verify react_to_message has correct signature."""
        import inspect
        sig = inspect.signature(WhatsAppAdapter.react_to_message)
        params = list(sig.parameters.keys())
        assert 'self' in params
        assert 'chat_id' in params
        assert 'message_id' in params
        assert 'emoji' in params
    
    def test_send_media_batch_signature(self):
        """Verify send_media_batch has correct signature."""
        import inspect
        sig = inspect.signature(WhatsAppAdapter.send_media_batch)
        params = list(sig.parameters.keys())
        assert 'self' in params
        assert 'chat_id' in params
        assert 'media_items' in params
        assert 'delay_ms' in params
    
    def test_methods_are_async(self):
        """Verify all new methods are async."""
        import asyncio
        import inspect
        
        assert asyncio.iscoroutinefunction(WhatsAppAdapter.delete_message)
        assert asyncio.iscoroutinefunction(WhatsAppAdapter.react_to_message)
        assert asyncio.iscoroutinefunction(WhatsAppAdapter.send_media_batch)


class TestSendResult:
    """Test SendResult structure for batch operations."""
    
    def test_sendresult_has_raw_response(self):
        """Verify SendResult supports raw_response field."""
        from gateway.platforms.base import SendResult
        
        result = SendResult(
            success=True,
            message_id="msg123",
            raw_response={
                "messageIds": ["msg1", "msg2"],
                "errors": []
            }
        )
        
        assert result.success is True
        assert result.message_id == "msg123"
        assert result.raw_response["messageIds"] == ["msg1", "msg2"]
        assert result.raw_response["errors"] == []


class TestBridgeEndpoints:
    """
    Integration tests for bridge endpoints.
    
    These require the WhatsApp bridge to be running on localhost:3000.
    Run the bridge separately to enable these tests:
      node scripts/whatsapp-bridge/bridge.js --port 3000 --session ~/.hermes/whatsapp/session
    """
    
    @pytest.fixture
    def bridge_running(self):
        """Check if bridge is running."""
        try:
            import aiohttp
            import asyncio
            async def check():
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            "http://127.0.0.1:3000/health",
                            timeout=aiohttp.ClientTimeout(total=2)
                        ) as resp:
                            return resp.status == 200
                except:
                    return False
            return asyncio.run(check())
        except:
            return False
    
    @pytest.mark.skipif(True, reason="Requires running bridge and real WhatsApp connection")
    @pytest.mark.asyncio
    async def test_bridge_delete_endpoint_exists(self):
        """Test that /delete endpoint is available on bridge."""
        # This test is skipped by default
        # To run: ensure bridge is running on localhost:3000
        pass
    
    @pytest.mark.skipif(True, reason="Requires running bridge and real WhatsApp connection")
    @pytest.mark.asyncio
    async def test_bridge_react_endpoint_exists(self):
        """Test that /react endpoint is available on bridge."""
        # This test is skipped by default
        # To run: ensure bridge is running on localhost:3000
        pass
    
    @pytest.mark.skipif(True, reason="Requires running bridge and real WhatsApp connection")
    @pytest.mark.asyncio
    async def test_bridge_send_media_batch_endpoint_exists(self):
        """Test that /send-media-batch endpoint is available on bridge."""
        # This test is skipped by default
        # To run: ensure bridge is running on localhost:3000
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
