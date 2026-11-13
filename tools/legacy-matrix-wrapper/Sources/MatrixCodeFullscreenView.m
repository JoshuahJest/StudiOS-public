#import <AppKit/AppKit.h>
#import <IOKit/ps/IOPowerSources.h>
#import <IOKit/ps/IOPSKeys.h>
#import <ScreenSaver/ScreenSaver.h>

#import "AccentPolicy.h"

static NSString * const LegacySaverPath = @"/Library/Screen Savers/3D Matrix Code.saver";
static NSString * const DefaultsModule = @"com.studios.legacy-matrix-fullscreen";

@interface MatrixAccent : NSObject
@property CGFloat x;
@property CGFloat y;
@property CGFloat speed;
@property NSInteger length;
@property NSUInteger seed;
@end
@implementation MatrixAccent
@end

@interface AccentOverlayView : NSView
@property(nonatomic, strong) NSMutableArray<MatrixAccent *> *accents;
@property(nonatomic, strong) ScreenSaverDefaults *defaults;
@property NSTimeInterval frameInterval;
- (void)tick;
@end

@implementation AccentOverlayView

- (instancetype)initWithFrame:(NSRect)frame {
    self = [super initWithFrame:frame];
    if (self) {
        _accents = [NSMutableArray array];
        _defaults = [ScreenSaverDefaults defaultsForModuleWithName:DefaultsModule];
        [_defaults registerDefaults:@{
            @"AccentEnabled": @YES,
            @"AccentFrequency": @20.0,
            @"AccentMode": @0,
            @"AccentR": @0.0,
            @"AccentG": @0.75,
            @"AccentB": @1.0,
        }];
        _frameInterval = 1.0 / 30.0;
        self.wantsLayer = YES;
        self.layer.backgroundColor = NSColor.clearColor.CGColor;
    }
    return self;
}

- (BOOL)isOpaque { return NO; }
- (BOOL)acceptsFirstMouse:(NSEvent *)event { return NO; }

- (void)tick {
    if (![self.defaults boolForKey:@"AccentEnabled"]) return;
    double fps = 1.0 / MAX(self.frameInterval, 1.0 / 120.0);
    double frequency = MAX(0.5, [self.defaults doubleForKey:@"AccentFrequency"]);
    double probability = accent_spawn_probability(frequency, fps);
    double randomUnit = (double)arc4random() / (double)UINT32_MAX;
    if (randomUnit < probability && self.accents.count < 32) {
        MatrixAccent *accent = [MatrixAccent new];
        CGFloat glyphWidth = 15.0;
        NSInteger columns = MAX(1, (NSInteger)(NSWidth(self.bounds) / glyphWidth));
        accent.x = (CGFloat)arc4random_uniform((uint32_t)columns) * glyphWidth;
        accent.y = NSHeight(self.bounds) + 20.0;
        accent.speed = 120.0 + (CGFloat)arc4random_uniform(120);
        accent.length = 8 + (NSInteger)arc4random_uniform(12);
        accent.seed = arc4random();
        [self.accents addObject:accent];
    }

    NSMutableArray *expired = [NSMutableArray array];
    for (MatrixAccent *accent in self.accents) {
        accent.y -= accent.speed * self.frameInterval;
        if (accent.y < -400.0) [expired addObject:accent];
    }
    [self.accents removeObjectsInArray:expired];
    [self setNeedsDisplay:YES];
}

- (void)drawRect:(NSRect)dirtyRect {
    [super drawRect:dirtyRect];
    CGFloat r = [self.defaults doubleForKey:@"AccentR"];
    CGFloat g = [self.defaults doubleForKey:@"AccentG"];
    CGFloat b = [self.defaults doubleForKey:@"AccentB"];
    NSColor *color = [NSColor colorWithCalibratedRed:r green:g blue:b alpha:1.0];
    NSFont *font = [NSFont monospacedSystemFontOfSize:15.0 weight:NSFontWeightSemibold];
    NSInteger mode = [self.defaults integerForKey:@"AccentMode"];
    static NSString * const glyphs = @"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ#$%&*+-=<>/\\";

    for (MatrixAccent *accent in self.accents) {
        NSInteger count = mode == 0 ? 1 : accent.length;
        for (NSInteger row = 0; row < count; row++) {
            CGFloat alpha = row == 0 ? 1.0 : MAX(0.08, 0.78 * (1.0 - (CGFloat)row / (CGFloat)count));
            NSString *glyph = [glyphs substringWithRange:NSMakeRange((accent.seed + (NSUInteger)row * 17) % glyphs.length, 1)];
            NSDictionary *attrs = @{
                NSFontAttributeName: font,
                NSForegroundColorAttributeName: [color colorWithAlphaComponent:alpha]
            };
            [glyph drawAtPoint:NSMakePoint(accent.x, accent.y + row * 15.0) withAttributes:attrs];
        }
    }
}
@end

@interface MatrixCodeFullscreenView : ScreenSaverView
@property(nonatomic, strong) ScreenSaverView *legacyView;
@property(nonatomic, strong) AccentOverlayView *accentOverlay;
@property(nonatomic, strong) NSPanel *configPanel;
@property(nonatomic, strong) NSColorWell *colorWell;
@property(nonatomic, strong) NSPopUpButton *modePopup;
@property(nonatomic, strong) NSSlider *frequencySlider;
@property(nonatomic, strong) NSTextField *frequencyLabel;
@property(nonatomic, strong) NSButton *enabledCheckbox;
@property(nonatomic, strong) ScreenSaverDefaults *wrapperDefaults;
@property NSUInteger frameCounter;
@end

@implementation MatrixCodeFullscreenView

- (instancetype)initWithFrame:(NSRect)frame isPreview:(BOOL)isPreview {
    self = [super initWithFrame:frame isPreview:isPreview];
    if (!self) return nil;

    self.wrapperDefaults = [ScreenSaverDefaults defaultsForModuleWithName:DefaultsModule];
    [self.wrapperDefaults registerDefaults:@{
        @"AccentEnabled": @YES, @"AccentFrequency": @20.0, @"AccentMode": @0,
        @"AccentR": @0.0, @"AccentG": @0.75, @"AccentB": @1.0,
    }];

    NSBundle *legacyBundle = [NSBundle bundleWithPath:LegacySaverPath];
    NSError *error = nil;
    if (![legacyBundle loadAndReturnError:&error]) {
        NSLog(@"Unable to load legacy Matrix bundle: %@", error);
        return self;
    }
    Class legacyClass = NSClassFromString(@"Matrix3DSSView");
    if (!legacyClass || ![legacyClass isSubclassOfClass:[ScreenSaverView class]]) return self;

    self.legacyView = [[legacyClass alloc] initWithFrame:self.bounds isPreview:isPreview];
    [self addSubview:self.legacyView];
    self.accentOverlay = [[AccentOverlayView alloc] initWithFrame:self.bounds];
    [self addSubview:self.accentOverlay positioned:NSWindowAbove relativeTo:self.legacyView];
    [self resizeSubviews];
    [self updateAdaptiveFrameRate];
    return self;
}

- (void)configureLegacyOpenGLView:(NSView *)view {
    if ([view isKindOfClass:[NSOpenGLView class]]) {
        NSOpenGLView *glView = (NSOpenGLView *)view;
        glView.wantsBestResolutionOpenGLSurface = NO;
        [glView.openGLContext update];
    }
    for (NSView *child in view.subviews) [self configureLegacyOpenGLView:child];
}

- (void)resizeSubviews {
    self.legacyView.frame = self.bounds;
    self.legacyView.bounds = self.bounds;
    self.accentOverlay.frame = self.bounds;
    [self configureLegacyOpenGLView:self.legacyView];
}

- (void)setFrameSize:(NSSize)newSize { [super setFrameSize:newSize]; [self resizeSubviews]; }

- (BOOL)isOnACPower {
    CFDictionaryRef adapter = IOPSCopyExternalPowerAdapterDetails();
    BOOL onAC = adapter != NULL;
    if (adapter) CFRelease(adapter);
    return onAC;
}

- (void)updateAdaptiveFrameRate {
    NSProcessInfo *process = NSProcessInfo.processInfo;
    BOOL thermalOK = process.thermalState == NSProcessInfoThermalStateNominal;
    self.animationTimeInterval = adaptive_frame_interval([self isOnACPower], process.lowPowerModeEnabled, thermalOK);
    self.accentOverlay.frameInterval = self.animationTimeInterval;
}

- (void)startAnimation { [super startAnimation]; [self.legacyView startAnimation]; }
- (void)stopAnimation { [self.legacyView stopAnimation]; [super stopAnimation]; }
- (void)animateOneFrame {
    if ((self.frameCounter++ % 300) == 0) [self updateAdaptiveFrameRate];
    [self.legacyView animateOneFrame];
    [self.accentOverlay tick];
}

- (BOOL)hasConfigureSheet { return YES; }

- (NSTextField *)label:(NSString *)text frame:(NSRect)frame {
    NSTextField *label = [[NSTextField alloc] initWithFrame:frame];
    label.stringValue = text; label.editable = NO; label.bezeled = NO;
    label.drawsBackground = NO; label.selectable = NO;
    return label;
}

- (NSWindow *)configureSheet {
    if (self.configPanel) return self.configPanel;
    self.configPanel = [[NSPanel alloc] initWithContentRect:NSMakeRect(0, 0, 440, 285)
        styleMask:(NSWindowStyleMaskTitled) backing:NSBackingStoreBuffered defer:NO];
    self.configPanel.title = @"3D Matrix Code Fullscreen";
    NSView *content = self.configPanel.contentView;

    self.enabledCheckbox = [NSButton checkboxWithTitle:@"Enable occasional accent characters" target:nil action:nil];
    self.enabledCheckbox.frame = NSMakeRect(24, 230, 320, 24);
    self.enabledCheckbox.state = [self.wrapperDefaults boolForKey:@"AccentEnabled"] ? NSControlStateValueOn : NSControlStateValueOff;
    [content addSubview:self.enabledCheckbox];

    NSButton *originalOptions = [NSButton buttonWithTitle:@"Original Matrix Options…" target:self action:@selector(showOriginalConfiguration:)];
    originalOptions.frame = NSMakeRect(270, 226, 150, 30);
    [content addSubview:originalOptions];

    [content addSubview:[self label:@"Accent color:" frame:NSMakeRect(24, 184, 110, 24)]];
    self.colorWell = [[NSColorWell alloc] initWithFrame:NSMakeRect(142, 181, 74, 30)];
    self.colorWell.color = [NSColor colorWithCalibratedRed:[self.wrapperDefaults doubleForKey:@"AccentR"]
        green:[self.wrapperDefaults doubleForKey:@"AccentG"] blue:[self.wrapperDefaults doubleForKey:@"AccentB"] alpha:1.0];
    [content addSubview:self.colorWell];

    [content addSubview:[self label:@"Accent style:" frame:NSMakeRect(24, 139, 110, 24)]];
    self.modePopup = [[NSPopUpButton alloc] initWithFrame:NSMakeRect(142, 136, 250, 30) pullsDown:NO];
    [self.modePopup addItemsWithTitles:@[@"Leading character only", @"Leading character and trail"]];
    [self.modePopup selectItemAtIndex:[self.wrapperDefaults integerForKey:@"AccentMode"]];
    [content addSubview:self.modePopup];

    [content addSubview:[self label:@"Average frequency:" frame:NSMakeRect(24, 94, 120, 24)]];
    self.frequencySlider = [[NSSlider alloc] initWithFrame:NSMakeRect(142, 94, 210, 24)];
    self.frequencySlider.minValue = 1.0; self.frequencySlider.maxValue = 120.0;
    self.frequencySlider.doubleValue = [self.wrapperDefaults doubleForKey:@"AccentFrequency"];
    self.frequencySlider.target = self; self.frequencySlider.action = @selector(frequencyChanged:);
    [content addSubview:self.frequencySlider];
    self.frequencyLabel = [self label:@"" frame:NSMakeRect(355, 94, 70, 24)];
    [content addSubview:self.frequencyLabel];
    [self frequencyChanged:self.frequencySlider];

    NSButton *cancel = [NSButton buttonWithTitle:@"Cancel" target:self action:@selector(cancelConfiguration:)];
    cancel.frame = NSMakeRect(250, 24, 80, 32); [content addSubview:cancel];
    NSButton *save = [NSButton buttonWithTitle:@"Save" target:self action:@selector(saveConfiguration:)];
    save.frame = NSMakeRect(338, 24, 80, 32); save.keyEquivalent = @"\r"; [content addSubview:save];
    return self.configPanel;
}

- (void)showOriginalConfiguration:(id)sender {
    NSWindow *legacySheet = [self.legacyView configureSheet];
    if (!legacySheet) { NSBeep(); return; }
    NSWindow *parent = self.configPanel.sheetParent ?: NSApp.mainWindow;
    [NSApp endSheet:self.configPanel returnCode:NSModalResponseCancel];
    dispatch_async(dispatch_get_main_queue(), ^{
        if (parent) {
            [parent beginSheet:legacySheet completionHandler:nil];
        } else {
            [legacySheet makeKeyAndOrderFront:nil];
        }
    });
}

- (void)frequencyChanged:(id)sender {
    self.frequencyLabel.stringValue = [NSString stringWithFormat:@"%.0f sec", self.frequencySlider.doubleValue];
}

- (void)saveConfiguration:(id)sender {
    NSColor *c = [self.colorWell.color colorUsingColorSpace:NSColorSpace.sRGBColorSpace];
    [self.wrapperDefaults setBool:self.enabledCheckbox.state == NSControlStateValueOn forKey:@"AccentEnabled"];
    [self.wrapperDefaults setInteger:self.modePopup.indexOfSelectedItem forKey:@"AccentMode"];
    [self.wrapperDefaults setDouble:self.frequencySlider.doubleValue forKey:@"AccentFrequency"];
    [self.wrapperDefaults setDouble:c.redComponent forKey:@"AccentR"];
    [self.wrapperDefaults setDouble:c.greenComponent forKey:@"AccentG"];
    [self.wrapperDefaults setDouble:c.blueComponent forKey:@"AccentB"];
    [self.wrapperDefaults synchronize];
    [NSApp endSheet:self.configPanel returnCode:NSModalResponseOK];
}

- (void)cancelConfiguration:(id)sender { [NSApp endSheet:self.configPanel returnCode:NSModalResponseCancel]; }

@end
