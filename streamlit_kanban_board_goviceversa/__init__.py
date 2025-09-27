import os
import streamlit.components.v1 as components

__version__ = "1.1.4"
__author__ = "Pierluigi Segatto"
__email__ = "pier@goviceversa.com"

# Export the main function
__all__ = ["kanban_board"]

_RELEASE = True

if not _RELEASE:
    _component_func = components.declare_component(
        "streamlit_kanban_board_goviceversa",
        url="http://localhost:3001",
    )
else:
    parent_dir = os.path.dirname(os.path.abspath(__file__))
    build_dir = os.path.join(parent_dir, "frontend/build")
    _component_func = components.declare_component("streamlit_kanban_board_goviceversa", path=build_dir)

def kanban_board(
    stages,
    deals,
    key=None,
    height=600,
    allow_empty_stages=True,
    draggable_stages=None,
    user_info=None,
    drag_validation_function=None,
    drag_restrictions=None,
    permission_matrix=None,
    business_rules=None,
    show_tooltips=True
):
    """
    Create a Kanban board for deal pipeline management.
    
    Parameters
    ----------
    stages : list of str or list of dict
        Column definitions. Can be:
        - List of strings: ["Stage 1", "Stage 2", "Stage 3"]
        - List of dicts: [{"id": "stage1", "name": "Initial Review", "color": "#e3f2fd"}]
    
    deals : list of dict
        Deal data. Each deal should have:
        - id: unique identifier
        - stage: current stage (must match stage id/name)
        - deal_id: deal identifier for display
        - company_name: company name
        - product_type: product type (shown as badge)
        - date: relevant date (e.g., submission date)
        - underwriter: underwriter name
        - custom_html: (optional) additional HTML content below defaults
        
        Example:
        {
            "id": "deal_123",
            "stage": "initial_review", 
            "deal_id": "D-2024-001",
            "company_name": "Acme Corp",
            "product_type": "Term Loan",
            "date": "2024-01-15",
            "underwriter": "John Smith",
            "custom_html": "<div class='priority-high'>High Priority</div>"
        }
    
    key : str
        Unique key for the component
    
    height : int
        Height of the kanban board in pixels
        
    allow_empty_stages : bool
        Whether to show stages with no deals
        
    draggable_stages : list of str, optional
        List of stage IDs that the current user can drag deals to/from.
        If None, all stages are draggable (default behavior).
        Use empty list [] to disable all dragging.
        
    user_info : dict, optional
        Current user information for permission checks:
        {
            "role": "riskManager",
            "email": "user@example.com",
            "permissions": ["risk_approval", "management_approval"],
            "approval_limits": {"VV": 100000, "OF": 150000}
        }
        
    drag_validation_function : callable, optional
        Custom function to validate drag operations. Should accept:
        (deal_data, from_stage, to_stage, user_info) -> dict
        Returns: {"allowed": bool, "reason": str, "warning": str}
        
            drag_restrictions : dict, optional
        Pre-computed drag restrictions for each deal:
        {
            "deal_id": {
                "draggable": bool,
                "allowed_stages": ["stage1", "stage2"],
                "blocked_stages": ["stage3"],
                "reasons": {"stage3": "Amount exceeds approval limit"}
            }
        }
        
    permission_matrix : dict, optional
        Role-based permission matrix defining what each role can do:
        {
            "role_name": {
                "stages": {
                    "stage_id": {
                        "view": bool,
                        "drag_to": bool,
                        "drag_from": bool,
                        "approve": bool,
                        "reject": bool,
                        "edit": bool,
                        "delete": bool,
                        "conditions": [...]
                    }
                },
                "actions": {
                    "create_deal": bool,
                    "delete_deal": bool,
                    "edit_deal": bool,
                    "approve_deal": bool,
                    "reject_deal": bool,
                    "request_info": bool,
                    "override_rules": bool,
                    "view_all_deals": bool,
                    "export_data": bool
                },
                "approval_limits": {
                    "VV": {"EUR": 100000},
                    "OF": {"EUR": 150000}
                }
            }
        }
        
    business_rules : list, optional
        List of business rules that govern deal transitions:
        [
            {
                "id": "rule1",
                "name": "VV High Amount Rule",
                "description": "VV deals >= 100k EUR require risk manager approval",
                "conditions": [
                    {"field": "source", "operator": "equals", "value": "VV"},
                    {"field": "amount", "operator": "greater_than", "value": 100000}
                ],
                "actions": [
                    {"type": "deny", "message": "Requires risk manager approval"}
                ],
                "priority": 100,
                "is_active": True
            }
        ]
        
    show_tooltips : bool, optional
        Whether to show tooltips with drag feedback messages. Default: True
    
    Returns
    -------
    dict
        Component state with:
        - deals: updated deals list with new stages
        - moved_deal: info about last moved deal (if any)
        - clicked_deal: info about last clicked deal (if any)
        - validation_error: info about blocked moves (if any)
    """
    
    # Normalize stages format
    normalized_stages = []
    for stage in stages:
        if isinstance(stage, str):
            normalized_stages.append({"id": stage, "name": stage, "color": None})
        else:
            normalized_stages.append({
                "id": stage.get("id", stage.get("name", "")),
                "name": stage.get("name", stage.get("id", "")),
                "color": stage.get("color", None)
            })
    
    # Validate deals have required fields
    for deal in deals:
        required_fields = ["id", "stage", "deal_id", "company_name"]
        missing_fields = [field for field in required_fields if field not in deal]
        if missing_fields:
            raise ValueError(f"Deal {deal.get('id', 'unknown')} missing required fields: {missing_fields}")
    
    # Handle draggable_stages parameter
    if draggable_stages is None:
        # Default behavior - all stages are draggable
        draggable_stages = [stage["id"] for stage in normalized_stages]
    elif draggable_stages == []:
        # Explicitly disable all dragging
        draggable_stages = []
    
    # Prepare drag restrictions data
    prepared_drag_restrictions = drag_restrictions or {}
    
    # Add user-specific drag validation if provided
    if user_info and drag_validation_function:
        # Pre-compute restrictions for all deals
        for deal in deals:
            deal_id = deal["id"]
            if deal_id not in prepared_drag_restrictions:
                prepared_drag_restrictions[deal_id] = {
                    "draggable": True,
                    "allowed_stages": [],
                    "blocked_stages": [],
                    "reasons": {}
                }
            
            # Check each stage for this deal
            for stage in normalized_stages:
                if stage["id"] != deal["stage"]:  # Don't check current stage
                    validation_result = drag_validation_function(
                        deal, deal["stage"], stage["id"], user_info
                    )
                    
                    if validation_result.get("allowed", True):
                        prepared_drag_restrictions[deal_id]["allowed_stages"].append(stage["id"])
                    else:
                        prepared_drag_restrictions[deal_id]["blocked_stages"].append(stage["id"])
                        prepared_drag_restrictions[deal_id]["reasons"][stage["id"]] = validation_result.get("reason", "Not authorized")
    
    component_value = _component_func(
        stages=normalized_stages,
        deals=deals,
        height=height,
        allow_empty_stages=allow_empty_stages,
        draggable_stages=draggable_stages,
        user_info=user_info,
        drag_restrictions=prepared_drag_restrictions,
        permission_matrix=permission_matrix,
        business_rules=business_rules,
        show_tooltips=show_tooltips,
        key=key,
        default={"deals": deals, "moved_deal": None, "clicked_deal": None, "validation_error": None}
    )
    
    return component_value 