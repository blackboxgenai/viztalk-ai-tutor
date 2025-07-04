"""
Enhanced Chat API with new database structure
"""
from fastapi import APIRouter, HTTPException, Query, Body, Request
from fastapi.responses import StreamingResponse
from services.chat_service import chat_service
from services.learning_service import learning_service
from models.schemas import MessageType
import json
import asyncio
import logging

logger = logging.getLogger(__name__)

chat_router = APIRouter()

@chat_router.post("/ask")
async def chat_endpoint(
    request: Request,
    user_prompt: str = Body(...),
    username: str = Body(...),
    isQuiz: bool = Body(False),
    isLearningPath: bool = Body(False)
):
    """Enhanced chat endpoint with new database structure"""
    try:
        logger.info(f"👤 User: {user_prompt} | 🆔 Username: {username}")
        
        # Get client info for session
        client_ip = request.client.host
        user_agent = request.headers.get("user-agent", "Unknown")
        
        # Create or get session
        session_id = await chat_service.create_session(username, client_ip, user_agent)
        
        # Determine message type
        message_type = MessageType.CONTENT
        if isLearningPath:
            message_type = MessageType.LEARNING_PATH
        elif isQuiz:
            message_type = MessageType.QUIZ
        
        # Store user message
        await chat_service.store_message(
            username=username,
            session_id=session_id,
            role="user",
            content=user_prompt,
            message_type=message_type
        )
        
        # Process based on type
        if isLearningPath:
            return await process_learning_path_request(username, session_id, user_prompt)
        elif isQuiz:
            return await process_quiz_request(username, session_id, user_prompt)
        else:
            return await process_chat_request(username, session_id, user_prompt)
            
    except Exception as e:
        logger.error(f"❌ Chat error: {str(e)}")
        raise HTTPException(status_code=500, detail="Chat service temporarily unavailable")

async def process_chat_request(username: str, session_id: str, user_prompt: str):
    """Process regular chat request with streaming"""
    try:
        # Import here to avoid circular imports
        from chat import generate_chat_stream
        from constants import get_basic_environment_prompt
        
        # Get recent chat history for context
        history_result = await chat_service.get_chat_history(username, session_id, limit=10)
        recent_messages = []
        
        if history_result.success:
            recent_messages = [
                {"role": msg["role"], "content": msg["content"]}
                for msg in history_result.data["messages"]
                if msg.get("message_type") != "learning_path"
            ]
        
        # Get user preferences for language
        from services.user_service import user_service
        user = await user_service.get_user_by_username(username)
        user_language = "English"  # Default to English
        if user and "preferences" in user:
            user_language = user["preferences"].get("language", "English")
        
        # Add current prompt with language-specific environment
        language_specific_prompt = get_basic_environment_prompt(user_language)
        enhanced_prompt = f"{user_prompt} {language_specific_prompt}"
        recent_messages.append({"role": "user", "content": enhanced_prompt})
        
        async def chat_stream():
            response_content = ""
            try:
                async for token in generate_chat_stream(recent_messages):
                    if token:
                        response_content += token
                        yield token
                
                # Store assistant response
                await chat_service.store_message(
                    username=username,
                    session_id=session_id,
                    role="assistant",
                    content=response_content,
                    message_type=MessageType.CONTENT
                )
                
            except Exception as e:
                logger.error(f"Streaming error: {e}")
                error_message = "I'm experiencing technical difficulties. Please try again later."
                yield error_message
                
                await chat_service.store_message(
                    username=username,
                    session_id=session_id,
                    role="assistant",
                    content=error_message,
                    message_type=MessageType.CONTENT
                )
        
        return StreamingResponse(chat_stream(), media_type="text/plain")
        
    except Exception as e:
        logger.error(f"Chat processing error: {e}")
        raise HTTPException(status_code=500, detail="Failed to process chat request")

async def process_learning_path_request(username: str, session_id: str, user_prompt: str):
    """Process learning path generation request"""
    try:
        # Import here to avoid circular imports
        from learning_path import process_learning_path_query
        from chat import generate_response
        from utils import extract_json
        from constants import LEARNING_PATH_PROMPT, REGENRATE_OR_FILTER_JSON
        
        # Get user preferences for learning path generation
        from services.user_service import user_service
        user = await user_service.get_user_by_username(username)
        preferences = user.get("preferences", {}) if user else {}
        
        # Format prompt with preferences
        prompt_with_preference = LEARNING_PATH_PROMPT.format(
            userRole=preferences.get("user_role", "Student"),
            timeValue=preferences.get("time_value", 15),
            language=preferences.get("language", "English"),
            ageGroup=preferences.get("age_group", "Above 18")
        )
        
        # Store chat history function
        async def store_chat_history(username, message):
            await chat_service.store_message(
                username=username,
                session_id=session_id,
                role=message["role"],
                content=message["content"],
                message_type=MessageType.LEARNING_PATH
            )
        
        # Process learning path query
        result = await process_learning_path_query(
            user_prompt, username, generate_response, extract_json,
            store_chat_history, REGENRATE_OR_FILTER_JSON, prompt_with_preference
        )
        
        # If successful, create learning goal
        if result.get("response") == "JSON" and result.get("content"):
            # Store the learning path content as a JSON string to ensure consistency
            content_to_store = result["content"]
            if isinstance(content_to_store, dict):
                # Store the content as a serialized JSON string
                await chat_service.store_message(
                    username=username,
                    session_id=session_id,
                    role="assistant",
                    content=json.dumps(content_to_store),
                    message_type=MessageType.LEARNING_PATH
                )
            
            learning_goal_data = {
                "name": result["content"].get("name", "AI Generated Learning Path"),
                "description": result["content"].get("description", ""),
                "duration": result["content"].get("course_duration", ""),
                "study_plans": [result["content"]],
                "tags": result["content"].get("tags", [])
            }
            
            await learning_service.create_learning_goal(username, learning_goal_data)
        
        return result
        
    except Exception as e:
        logger.error(f"Learning path processing error: {e}")
        raise HTTPException(status_code=500, detail="Failed to process learning path request")

async def process_quiz_request(username: str, session_id: str, user_prompt: str):
    """Process quiz generation request with streaming response"""
    try:
        # Import here to avoid circular imports
        from chat import generate_chat_stream
        from constants import CALCULATE_SCORE, get_basic_environment_prompt
        
        # Check if this is quiz generation vs quiz submission
        user_prompt_lower = user_prompt.lower()
        is_quiz_generation = any(keyword in user_prompt_lower for keyword in [
            'generate', 'create', 'make', 'quiz about', 'quiz on', 'quiz for',
            'give me a quiz', 'start a quiz', 'new quiz'
        ])
        
        # Get recent chat history for context
        history_result = await chat_service.get_chat_history(username, session_id, limit=10)
        recent_messages = []
        
        if history_result.success:
            recent_messages = [
                {"role": msg["role"], "content": msg["content"]}
                for msg in history_result.data["messages"]
                if msg.get("message_type") != "learning_path"
            ]
        
        # Get user preferences for language
        from services.user_service import user_service
        user = await user_service.get_user_by_username(username)
        user_language = "English"  # Default to English
        if user and "preferences" in user:
            user_language = user["preferences"].get("language", "English")
        
        # Build enhanced prompt
        if is_quiz_generation:
            # For quiz generation, just add language preference
            language_specific_prompt = get_basic_environment_prompt(user_language)
            enhanced_prompt = f"{user_prompt} {language_specific_prompt}"
        else:
            # For quiz submission/grading, add CALCULATE_SCORE
            enhanced_prompt = f"{user_prompt} {CALCULATE_SCORE}"
        
        # Add current prompt to messages
        recent_messages.append({"role": "user", "content": enhanced_prompt})
        
        async def quiz_stream():
            response_content = ""
            try:
                async for token in generate_chat_stream(recent_messages):
                    if token:
                        response_content += token
                        yield token
                
                # Store assistant response
                await chat_service.store_message(
                    username=username,
                    session_id=session_id,
                    role="assistant",
                    content=response_content,
                    message_type=MessageType.QUIZ
                )
                
            except Exception as e:
                logger.error(f"Quiz streaming error: {e}")
                error_message = "I'm experiencing technical difficulties generating the quiz. Please try again later."
                yield error_message
                
                await chat_service.store_message(
                    username=username,
                    session_id=session_id,
                    role="assistant",
                    content=error_message,
                    message_type=MessageType.QUIZ
                )
        
        return StreamingResponse(quiz_stream(), media_type="text/plain")
        
    except Exception as e:
        logger.error(f"Quiz processing error: {e}")
        raise HTTPException(status_code=500, detail="Failed to process quiz request")

@chat_router.get("/history")
async def get_chat_history(username: str = Query(...), limit: int = Query(50)):
    """Get chat history for user"""
    try:
        result = await chat_service.get_chat_history(username, limit=limit)
        
        if not result.success:
            raise HTTPException(status_code=404, detail=result.message)
        
        return {"history": result.data["messages"]}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"History error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch chat history")

@chat_router.post("/store-quiz")
async def store_quiz_messages(
    request: Request,
    username: str = Body(...),
    user_message: dict = Body(...),
    quiz_message: dict = Body(...)
):
    """Store quiz messages directly to chat history for persistence"""
    try:
        logger.info(f"💾 Storing quiz messages for user: {username}")
        
        # Get client info for session
        client_ip = request.client.host
        user_agent = request.headers.get("user-agent", "Unknown")
        
        # Create or get session
        session_id = await chat_service.create_session(username, client_ip, user_agent)
        
        # Store user message first
        user_result = await chat_service.store_message(
            username=username,
            session_id=session_id,
            role=user_message.get("role", "user"),
            content=user_message.get("content", ""),
            message_type=MessageType.CONTENT
        )
        
        if not user_result.success:
            logger.error(f"Failed to store user message: {user_result.message}")
            raise HTTPException(status_code=500, detail="Failed to store user message")
        
        # Store quiz message with proper type
        quiz_result = await chat_service.store_message(
            username=username,
            session_id=session_id,
            role=quiz_message.get("role", "assistant"),
            content=quiz_message.get("content", {}),
            message_type=MessageType.QUIZ
        )
        
        if not quiz_result.success:
            logger.error(f"Failed to store quiz message: {quiz_result.message}")
            raise HTTPException(status_code=500, detail="Failed to store quiz message")
        
        logger.info(f"✅ Successfully stored quiz messages for user: {username}")
        return {
            "success": True,
            "message": "Quiz messages stored successfully",
            "user_message_id": user_result.data.get("message_id"),
            "quiz_message_id": quiz_result.data.get("message_id")
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Store quiz messages error: {e}")
        raise HTTPException(status_code=500, detail="Failed to store quiz messages")

@chat_router.delete("/clear")
async def clear_chat_history(username: str = Query(...)):
    """Clear chat history for user"""
    try:
        result = await chat_service.clear_chat_history(username)
        
        if not result.success:
            raise HTTPException(status_code=500, detail=result.message)
        
        return {"message": result.message}
        
    except Exception as e:
        logger.error(f"Clear history error: {e}")
        raise HTTPException(status_code=500, detail="Failed to clear chat history")

@chat_router.get("/search")
async def search_messages(username: str = Query(...), query: str = Query(...)):
    """Search messages using full-text search"""
    try:
        result = await chat_service.search_messages(username, query)
        
        if not result.success:
            raise HTTPException(status_code=500, detail=result.message)
        
        return result.data
        
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail="Failed to search messages")

@chat_router.get("/analytics")
async def get_chat_analytics(username: str = Query(...), days: int = Query(30)):
    """Get chat analytics for user"""
    try:
        result = await chat_service.get_message_analytics(username, days)
        
        if not result.success:
            raise HTTPException(status_code=500, detail=result.message)
        
        return result.data
        
    except Exception as e:
        logger.error(f"Analytics error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get analytics")

@chat_router.post("/archive")
async def archive_old_messages(days_old: int = Body(90)):
    """Archive old messages (admin only)"""
    try:
        result = await chat_service.archive_old_messages(days_old)
        
        if not result.success:
            raise HTTPException(status_code=500, detail=result.message)
        
        return result.data
        
    except Exception as e:
        logger.error(f"Archive error: {e}")
        raise HTTPException(status_code=500, detail="Failed to archive messages")