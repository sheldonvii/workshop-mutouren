"""姿势库：每个姿势是一条关键点几何规则。

左右指玩家自身的左右（COCO 定义）。图像坐标 y 向下。规则返回 True（摆对）、False（摆错）或
None（所需关键点看不清）。长度都以躯干长度为单位，见 vision.body_scale。
"""
from .vision import L_ELBOW, L_HIP, L_SHOULDER, L_WRIST, R_ELBOW, R_HIP, R_SHOULDER, R_WRIST, body_scale


def _ctx(person, vis_min, required):
    if not all(person.visible(i, vis_min) for i in required):
        return None
    k = person.kps
    mid_x = (k[L_SHOULDER, 0] + k[R_SHOULDER, 0]) / 2
    shoulder_y = min(k[L_SHOULDER, 1], k[R_SHOULDER, 1])
    return k, mid_x, shoulder_y, body_scale(person, vis_min)


def _above_head(k, wrist, shoulder_y, scale):
    return k[wrist, 1] < shoulder_y - 0.5 * scale


def _arm_down_or_hidden(person, k, wrist, shoulder, vis_min):
    return not person.visible(wrist, vis_min) or k[wrist, 1] > k[shoulder, 1]


def raise_one(side):
    wrist, other_wrist, other_shoulder = ((L_WRIST, R_WRIST, R_SHOULDER) if side == 'left'
                                          else (R_WRIST, L_WRIST, L_SHOULDER))

    def rule(person, vis_min):
        ctx = _ctx(person, vis_min, (L_SHOULDER, R_SHOULDER, wrist))
        if ctx is None:
            return None
        k, _, shoulder_y, scale = ctx
        return bool(_above_head(k, wrist, shoulder_y, scale)
                    and _arm_down_or_hidden(person, k, other_wrist, other_shoulder, vis_min))
    return rule


def both_up(person, vis_min):
    ctx = _ctx(person, vis_min, (L_SHOULDER, R_SHOULDER, L_WRIST, R_WRIST))
    if ctx is None:
        return None
    k, _, shoulder_y, scale = ctx
    return bool(_above_head(k, L_WRIST, shoulder_y, scale) and _above_head(k, R_WRIST, shoulder_y, scale))


def hands_on_hips(person, vis_min):
    ctx = _ctx(person, vis_min, (L_SHOULDER, R_SHOULDER, L_WRIST, R_WRIST, L_ELBOW, R_ELBOW))
    if ctx is None:
        return None
    k, mid_x, _, scale = ctx
    for wrist, elbow, shoulder, hip in ((L_WRIST, L_ELBOW, L_SHOULDER, L_HIP), (R_WRIST, R_ELBOW, R_SHOULDER, R_HIP)):
        hip_y = k[hip, 1] if person.visible(hip, vis_min) else k[shoulder, 1] + scale
        half = abs(k[shoulder, 0] - mid_x)
        if abs(k[wrist, 1] - hip_y) > 0.45 * scale:
            return False
        if abs(k[wrist, 0] - mid_x) > half + 0.5 * scale:
            return False
        if abs(k[elbow, 0] - mid_x) <= half + 0.3 * scale:
            return False
    return True


def t_pose(person, vis_min):
    ctx = _ctx(person, vis_min, (L_SHOULDER, R_SHOULDER, L_WRIST, R_WRIST))
    if ctx is None:
        return None
    k, mid_x, _, scale = ctx
    for wrist, shoulder in ((L_WRIST, L_SHOULDER), (R_WRIST, R_SHOULDER)):
        side = 1 if k[shoulder, 0] > mid_x else -1
        if abs(k[wrist, 1] - k[shoulder, 1]) > 0.45 * scale:
            return False
        if (k[wrist, 0] - k[shoulder, 0]) * side < 0.9 * scale:
            return False
    return True


POSES = {
    'raise_left': {'name': '举起左手', 'rule': raise_one('left')},
    'raise_right': {'name': '举起右手', 'rule': raise_one('right')},
    'both_up': {'name': '双手举过头顶', 'rule': both_up},
    'hands_on_hips': {'name': '双手叉腰', 'rule': hands_on_hips},
    't_pose': {'name': '双臂平举', 'rule': t_pose},
}


def evaluate(pose_id, person, vis_min):
    return POSES[pose_id]['rule'](person, vis_min)


def catalog(ids):
    return [{'id': i, 'name': POSES[i]['name']} for i in ids if i in POSES]
