package com.traclabs.biosim.server.simulation.food;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

public class ShelfLampTest {

    private static final float LETTUCE_PPF = 295.14f;
    private static final float AREA = 10f;

    private static Shelf lettuceShelf() {
        return new Shelf(PlantType.LETTUCE, AREA, new BiomassPS());
    }

    @Test
    public void testDefaultLampsAreUnchanged() {
        Shelf shelf = lettuceShelf();
        assertEquals(261f * 4.68f, shelf.getPhotonEfficacy(), 1e-3f, "high pressure sodium efficacy");
        assertEquals(1520f * AREA, shelf.getPowerNeeded(), 1e-3f, "BVAD 1520 W/m2 draw");
        assertEquals(122.15f, shelf.deliveredPPF(1f), 0.01f);
        assertEquals(LETTUCE_PPF, shelf.deliveredPPF(1000f), 0.01f, "saturates at what the crop needs");
    }

    @Test
    public void testLampEfficacyMakesLightProportionalToRealisticPower() {
        Shelf shelf = lettuceShelf();
        shelf.setLampEfficacy(4.05f);
        float full = LETTUCE_PPF * AREA / 4.05f;   // about 729 W
        assertEquals(4.05f, shelf.getPhotonEfficacy(), 1e-6f);
        assertEquals(LETTUCE_PPF, shelf.deliveredPPF(full), 0.01f);
        assertEquals(LETTUCE_PPF / 2f, shelf.deliveredPPF(full / 2f), 0.01f, "dimming to half power halves the light");
        assertEquals(LETTUCE_PPF, shelf.deliveredPPF(2000f), 0.01f, "power beyond full light adds no light");
        assertEquals(1520f * AREA, shelf.getPowerNeeded(), 1e-3f, "still draws up to the BVAD limit (backup heat)");
    }

    @Test
    public void testNonPositiveEfficacyFallsBackToDefaultLamps() {
        Shelf shelf = lettuceShelf();
        shelf.setLampEfficacy(0f);
        assertEquals(261f * 4.68f, shelf.getPhotonEfficacy(), 1e-3f);
        assertEquals(1520f * AREA, shelf.getPowerNeeded(), 1e-3f);
    }
}
