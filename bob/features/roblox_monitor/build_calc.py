import math

def calculate_stats(strength: int) -> dict:
    def get_fireball_dmg(str_val):
        if str_val >= 423: return 110
        if str_val >= 378: return 100
        if str_val >= 350: return 90
        if str_val >= 300: return 80
        if str_val >= 113: return 40
        if str_val >= 68: return 30
        if str_val >= 25: return 20
        return 10

    def get_fire_breathe_dmg(str_val):
        if str_val >= 419: return 9
        if str_val >= 365: return 8
        if str_val >= 325: return 7
        if str_val >= 300: return 6
        return 5

    def get_sun_barrage_dmg(str_val):
        if str_val >= 385: return 55
        if str_val >= 350: return 50
        if str_val >= 300: return 45
        if str_val >= 275: return 40
        return 35

    def get_fly_kick_range(str_val):
        if str_val >= 419: return 32
        if str_val >= 385: return 31
        if str_val >= 360: return 30
        if str_val >= 325: return 29
        if str_val >= 300: return 28
        if str_val >= 275: return 27
        return 26
        
    speed = round(130 + 0.30 * strength)
    
    return {
        "strength_allocated": strength,
        "universal_move_speed": speed,
        "moves": {
            "Fireball": {
                "damage": get_fireball_dmg(strength),
                "speed": speed,
                "upgraded_damage": round(get_fireball_dmg(strength) * 1.2, 1),
                "upgraded_speed": round(speed * 1.15, 1)
            },
            "Swing Kick": {
                "damage": round(10 + 0.21 * strength),
                "speed": speed,
                "upgraded_damage": round(round(10 + 0.21 * strength) * 1.2, 1),
                "upgraded_speed": round(speed * 1.15, 1)
            },
            "Fly Kick": {
                "damage": round(20 + 0.13 * strength),
                "range": get_fly_kick_range(strength),
                "upgraded_damage": round(round(20 + 0.13 * strength) * 1.2, 1)
            },
            "Fire Breathe": {
                "damage": get_fire_breathe_dmg(strength),
                "speed": speed,
                "upgraded_damage": round(get_fire_breathe_dmg(strength) * 1.2, 1),
                "upgraded_speed": round(speed * 1.15, 1)
            },
            "Sun Blast": {
                "damage": round(15 + 0.30 * strength),
                "speed": speed,
                "upgraded_damage": round(round(15 + 0.30 * strength) * 1.2, 1),
                "upgraded_speed": round(speed * 1.15, 1)
            },
            "Sun Barrage": {
                "damage": get_sun_barrage_dmg(strength),
                "speed": speed,
                "upgraded_damage": round(get_sun_barrage_dmg(strength) * 1.2, 1),
                "upgraded_speed": round(speed * 1.15, 1)
            },
            "Fire Fly": {
                "duration_seconds": round((strength / 14) + 1.5),
                "speed": 65
            }
        }
    }
